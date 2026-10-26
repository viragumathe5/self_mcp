"""Generated knowledge is live, read-only, complete, and scoped to one project."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from mcp.types import CallToolRequest, CallToolRequestParams

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import api
import mcp_knowledge as knowledge
import mcp_server
from task_store import TaskStore
from task_models import RunInput
from providers import ProviderError


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = TaskStore(Path(temp.name) / 'tasks.json')
        self.project, self.other, self.turn = (str(uuid4()) for _ in range(3))
        self.project_info = {'id': self.project, 'slug': 'knowledge', 'name': 'Knowledge'}
        for patcher in (patch.object(knowledge, 'store', self.store),
                        patch.object(knowledge.storage, 'get_store', side_effect=lambda sid: {'id': sid} if sid in (self.project, self.other) else None),
                        patch.object(mcp_server.storage, 'get_store_by_slug', side_effect=lambda slug: self.project_info if slug == 'knowledge' else None),
                        patch.object(mcp_server.storage, 'list_documents', return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.run = self.store.create(self.project, RunInput(goal='Build importer', provider='openai').model_dump())
        self.long_output = 'def import_rows():\n    return "λ CSV"\n' * 1700
        def saved(run):
            run.update(status='failed', error='Review did not finish',
                tasks=[{'id': 'parser', 'title': 'CSV parser', 'provider': 'openai', 'model': 'gpt-5-mini',
                        'output': self.long_output, 'output_version': 2,
                        'output_versions': [{'version': 1, 'output': 'Initial parser draft'},
                                            {'version': 2, 'output': self.long_output}]}],
                reviews=[{'approved': False, 'feedback': 'Check empty inputs', 'revision_tasks': ['parser'],
                          'output_versions': {'parser': 1},
                          'task_reviews': [{'task_id': 'parser', 'verdict': 'revise', 'reason': 'Missing boundary case'}]}],
                turns=[{'turn_id': self.turn, 'goal': 'Earlier request', 'status': 'completed',
                        'final_output': 'Earlier final answer', 'review_approved': True}])
        self.store.update(self.project, self.run['id'], saved)
        other = self.store.create(self.other, RunInput(goal='Private other project', provider='openai').model_dump())
        self.store.update(self.other, other['id'], lambda run: run.update(status='completed', final_output='Other project secret result'))

    def listed(self, **kwargs):
        return knowledge.list_results(self.project, knowledge.ListResults(**kwargs))

    def test_old_and_partial_results_are_available_without_export_or_writes(self):
        original = self.store.path.read_bytes()
        result = self.listed()
        self.assertEqual(result['total'], 4)
        self.assertNotIn('Other project', json.dumps(result))
        drafts = [r for r in result['results'] if r['kind'] == 'worker_draft']
        self.assertEqual([r['version'] for r in drafts], [2, 1])
        self.assertEqual([r['review_status'] for r in drafts], ['unreviewed', 'revision_requested'])
        self.assertTrue(drafts[0]['preview_truncated'])
        final = next(r for r in result['results'] if r['kind'] == 'final_answer')
        self.assertFalse(final['is_current_message'])
        self.assertEqual(final['review_status'], 'approved')
        self.assertEqual(final['turn_id'], self.turn)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.listed(include_older_versions=False)['total'], 3)

    def test_pagination_search_full_content_and_chunked_read_are_lossless(self):
        first = self.listed(limit=2)
        second = self.listed(offset=first['next_offset'], limit=2)
        self.assertIsNone(second['next_offset'])
        self.assertEqual(len({r['result_id'] for r in first['results'] + second['results']}), 4)
        result = knowledge.call(self.project, 'search_task_results', {'query': 'λ CSV'})
        self.assertEqual(result['total'], 1)
        identifier = result['results'][0]['result_id']
        chunks, offset = [], 0
        while offset is not None:
            part = knowledge.read_result(self.project, knowledge.ReadResult(result_id=identifier, offset=offset, limit=9999))
            chunks.append(part['content'])
            offset = part['next_offset']
        self.assertEqual(''.join(chunks), self.long_output)
        self.assertEqual(self.listed(run_id='missing')['total'], 0)

    def test_cross_project_ids_and_resource_uris_cannot_read_content(self):
        result = self.listed()['results'][0]
        with self.assertRaises(ProviderError):
            knowledge.read_result(self.other, knowledge.ReadResult(result_id=result['result_id']))
        with self.assertRaises(ValueError):
            knowledge.read_uri(self.other, result['uri'])
        for suffix in ('?offset=-1', '?limit=20001', '?offset=0&offset=1', '?unknown=x'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                knowledge.read_uri(self.project, result['uri'] + suffix)

    async def test_existing_mcp_server_sees_new_results_and_preserves_ids(self):
        server = mcp_server.make_mcp_server(self.project, 'Knowledge')
        async def listing():
            reply = await server.request_handlers[CallToolRequest](CallToolRequest(method='tools/call',
                params=CallToolRequestParams(name='list_task_results', arguments={})))
            self.assertFalse(reply.root.isError)
            return json.loads(reply.root.content[0].text)
        before = await listing()
        new = self.store.create(self.project, RunInput(goal='New work', provider='openai').model_dump())
        self.store.update(self.project, new['id'], lambda run: run.update(status='completed', final_output='New answer'))
        after = await listing()
        self.assertEqual(after['total'], before['total'] + 1)
        self.assertTrue({r['result_id'] for r in before['results']} < {r['result_id'] for r in after['results']})
        restarted = TaskStore(self.store.path)
        with patch.object(knowledge, 'store', restarted):
            self.assertEqual(self.listed(), after)

    async def test_http_catalog_resource_reads_and_document_compatibility(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://127.0.0.1:8000') as client:
            async def rpc(method, params=None):
                response = await client.post('/mcp/knowledge/mcp', headers={'Accept': 'application/json, text/event-stream'},
                    json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
                self.assertEqual(response.status_code, 200)
                return response.json()['result']
            catalog = await rpc('tools/list')
            self.assertTrue(knowledge.NAMES <= {t['name'] for t in catalog['tools']})
            search = await rpc('tools/call', {'name': 'search_project_knowledge', 'arguments': {'query': 'λ CSV'}})
            self.assertFalse(search.get('isError', False))
            found = json.loads(search['content'][0]['text'])
            self.assertEqual(found['project_id'], self.project)
            source = found['results'][0]
            expanded = await rpc('tools/call', {'name': 'get_project_knowledge', 'arguments': {'source_id': source['source_id'], 'limit': 100}})
            self.assertEqual(len(json.loads(expanded['content'][0]['text'])['content']), 100)
            wrong_scope = await rpc('tools/call', {'name': 'search_project_knowledge', 'arguments': {'query': 'secret', 'store_id': self.other}})
            self.assertTrue(wrong_scope['isError'])
            resources = await rpc('resources/list')
            uri = resources['resources'][0]['uri']
            index = await rpc('resources/read', {'uri': uri})
            self.assertEqual(index['contents'][0]['mimeType'], 'application/json')
            item = json.loads(index['contents'][0]['text'])['results'][0]
            read = await rpc('resources/read', {'uri': item['uri'] + '?offset=0&limit=100'})
            self.assertEqual(len(json.loads(read['contents'][0]['text'])['content']), 100)
            templates = await rpc('resources/templates/list')
            self.assertIn('task-result://', templates['resourceTemplates'][0]['uriTemplate'])
            with patch.object(mcp_server.storage, 'get_document', return_value={'title': 'Existing doc', 'content': 'Document knowledge'}):
                doc = await rpc('resources/read', {'uri': 'doc://existing'})
                self.assertIn('Document knowledge', doc['contents'][0]['text'])
