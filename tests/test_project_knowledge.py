"""Isolation, freshness, bounded evidence and zero-inference project retrieval."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import mcp_knowledge
import project_knowledge as knowledge
import storage
from providers import ProviderError
from task_models import RunInput
from task_store import TaskStore


class ProjectKnowledgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for item in (patch.object(storage, 'DATA_DIR', root), patch.object(storage, 'DATA_FILE', root / 'data.json')):
            item.start()
            self.addCleanup(item.stop)
        self.store = TaskStore(root / 'tasks.json')
        self.a, self.b = 'project-a', 'project-b'
        self.data = {'stores': [
            {'id': self.a, 'name': 'A', 'documents': [
                {'id': 'routing', 'title': 'Routing contract', 'content': 'The retry_budget is 2. Preserve completed worker outputs.', 'tags': []},
                {'id': 'deep', 'title': 'Long implementation', 'content': 'Unrelated background.\n' * 300 + '\ndef parseCSVRows(rows):\n    return "violet delimiter"\n', 'tags': []}],
             'links': [{'id': 'link', 'title': 'Provider reference', 'url': 'https://example.invalid', 'description': 'Endpoint supports streaming.'}]},
            {'id': self.b, 'name': 'B', 'documents': [{'id': 'secret', 'title': 'Routing contract', 'content': 'retry_budget secret-b-only-value', 'tags': []}], 'links': []}]}
        self.save()
        self.service = knowledge.KnowledgeService(self.store)

    def save(self):
        storage.DATA_FILE.write_text(json.dumps(self.data))

    def run_with_versions(self):
        run = self.store.create(self.a, RunInput(goal='CSV implementation', provider='openai').model_dump())
        def populate(value):
            value.update(tasks=[{'id': 'parser', 'title': 'CSV parser', 'output': 'csv delimiter handles quoted fields',
                'output_version': 2, 'output_versions': [{'version': 1, 'output': 'csv delimiter initial bug'},
                                                       {'version': 2, 'output': 'csv delimiter handles quoted fields'}]}],
                reviews=[{'approved': False, 'output_versions': {'parser': 1},
                          'task_reviews': [{'task_id': 'parser', 'verdict': 'revise', 'reason': 'Missing quoted fields'}]}])
        self.store.update(self.a, run['id'], populate)
        return run

    def test_ranked_passage_identifiers_and_source_offsets(self):
        result = self.service.search(self.a, 'parse CSV rows violet delimiter')
        source = result['results'][0]
        self.assertIn('parseCSVRows', source['excerpt'])
        self.assertGreater(source['offset'], 0)
        self.assertEqual(source['review_status'], 'user_supplied')
        read = self.service.read(self.a, source['source_id'], source['offset'], source['end_offset'] - source['offset'])
        self.assertEqual(read['content'], source['excerpt'])
        self.assertTrue(source['excerpt_truncated'])
        self.assertLessEqual(knowledge.token_count(result), 1500)
        self.assertEqual(result['tokens'], knowledge.token_count(result))
        self.assertTrue(self.service.search(self.a, 'retry budget')['results'])

    def test_cached_queries_and_reads_do_not_reload_files_or_call_a_model(self):
        first = self.service.search(self.a, 'retry_budget')
        source = first['results'][0]['source_id']
        with patch.object(storage, 'get_store', side_effect=AssertionError('No disk reload')),\
             patch.object(self.store, 'transaction', side_effect=AssertionError('No task reload')),\
             patch('task_models.infer', side_effect=AssertionError('No inference')),\
             patch.object(knowledge, 'chunks_for', side_effect=AssertionError('No reindex')):
            for _ in range(10):
                self.assertEqual(self.service.search(self.a, 'retry_budget')['results'], first['results'])
                self.assertIn('retry_budget', self.service.read(self.a, source)['content'])

    def test_project_isolation_for_search_ids_uris_and_forged_arguments(self):
        result = self.service.search(self.b, 'retry_budget')['results'][0]
        self.assertNotIn('secret-b-only-value', json.dumps(self.service.search(self.a, 'retry_budget')))
        self.assertFalse(self.service.search(self.a, 'secret-b-only-value')['results'])
        with self.assertRaises(ProviderError) as error:
            self.service.read(self.a, result['source_id'])
        self.assertEqual(error.exception.status, 404)
        with patch.object(mcp_knowledge, 'store', self.store):
            with self.assertRaises(ValueError):
                mcp_knowledge.read_uri(self.a, result['uri'])
            with self.assertRaises(ValueError):
                mcp_knowledge.call(self.a, 'search_project_knowledge', {'query': 'retry_budget', 'store_id': self.b})
            self.assertFalse(mcp_knowledge.call(self.a, 'search_project_knowledge', {'query': 'secret-b-only-value'})['results'])

    def test_edits_deletions_and_project_removal_invalidate_warm_cache(self):
        old = self.service.search(self.a, 'retry_budget')['results'][0]['source_id']
        self.data['stores'][0]['documents'][0]['content'] = 'The retry_budget is now 4.'
        self.save()
        new = self.service.search(self.a, 'retry_budget')['results'][0]
        self.assertIn('now 4', new['excerpt'])
        self.assertNotEqual(old, new['source_id'])
        with self.assertRaises(ProviderError):
            self.service.read(self.a, old)
        self.data['stores'][0]['documents'].pop(0)
        self.save()
        self.assertFalse(self.service.search(self.a, 'retry_budget')['results'])
        self.data['stores'].pop(0)
        self.save()
        with self.assertRaises(ProviderError) as error:
            self.service.search(self.a, 'violet')
        self.assertEqual(error.exception.status, 404)

    def test_invalid_storage_fails_closed_instead_of_returning_stale_evidence(self):
        self.service.search(self.a, 'retry_budget')
        storage.DATA_FILE.write_text('{')
        with self.assertRaises(ProviderError) as error:
            self.service.search(self.a, 'retry_budget')
        self.assertEqual(error.exception.status, 503)
        self.save()
        self.assertTrue(self.service.search(self.a, 'retry_budget')['results'])

    def test_worker_versions_exact_review_status_and_independent_process_refresh(self):
        run = self.run_with_versions()
        initial = self.service.search(self.a, 'csv delimiter', limit=20, token_budget=6000)
        draft = next(item for item in initial['results'] if item['kind'] == 'worker_draft')
        self.assertEqual(draft['version'], 2)
        self.assertEqual(draft['review_status'], 'unreviewed')
        older = self.service.search(self.a, 'initial bug', include_older_versions=True)
        self.assertEqual(older['results'][0]['review_status'], 'revision_requested')
        other_process = TaskStore(self.store.path)
        with patch.object(storage, 'get_store', side_effect=AssertionError('Task progress must not reload unchanged documents')):
            other_process.update(self.a, run['id'], lambda value: value.update(final_output='CSV delimiter final recovered answer'))
            result = self.service.search(self.a, 'recovered answer')
        self.assertEqual(result['results'][0]['kind'], 'final_answer')
        with patch.object(knowledge, 'chunks_for', side_effect=AssertionError('Unchanged text must be reused')):
            other_process.update(self.a, run['id'], lambda value: value.update(revision_count=1))
            refreshed = self.service.snapshot(self.a)
        self.assertEqual(refreshed.reused_sources, len(refreshed.sources))
        self.assertFalse(any(item.get('run_id') == run['id'] for item in self.service.search(
            self.a, 'csv', exclude_turn=(run['id'], run['turn_id']))['results']))

    def test_budgets_deduplication_no_match_and_no_storage_writes(self):
        duplicate = dict(self.data['stores'][0]['documents'][0], id='duplicate')
        self.data['stores'][0]['documents'].append(duplicate)
        self.save()
        before = storage.DATA_FILE.read_bytes()
        for budget in (256, 500, 1000, 1500, 6000):
            result = self.service.search(self.a, 'retry_budget', token_budget=budget)
            self.assertLessEqual(knowledge.token_count(result), budget)
            self.assertLessEqual(len(result['results']), 1)
        self.assertFalse(self.service.search(self.a, 'no_such_identifier_zz')['results'])
        self.assertEqual(self.service.search(self.a, 'the and please')['status'], 'no_matches')
        self.assertEqual(storage.DATA_FILE.read_bytes(), before)
        self.assertFalse(self.store.path.exists())

    def test_parallel_project_queries_never_mix_evidence(self):
        def query(project):
            for _ in range(10):
                result = self.service.search(project, 'retry_budget')
                self.assertEqual(result['project_id'], project)
                self.assertEqual('secret-b-only-value' in json.dumps(result), project == self.b)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(query, [self.a, self.b] * 3))

    def test_context_budget_preserves_required_payload(self):
        run = {'id': 'run', 'turn_id': 'turn', 'store_id': self.a, 'goal': 'retry_budget'}
        payload = {'dependencies': {'worker': 'Required implementation. ' * 400}}
        with patch.object(knowledge, 'service', return_value=self.service):
            packed = knowledge.for_model(self.store, run, 'instructions', payload, {'context_tokens': 2300}, 2048)
        self.assertEqual(packed['status'], 'skipped_budget')
        self.assertEqual(payload['dependencies']['worker'], 'Required implementation. ' * 400)

    def test_mcp_search_and_paged_read_use_same_sources_as_agents(self):
        with patch.object(mcp_knowledge, 'store', self.store):
            mcp = mcp_knowledge.call(self.a, 'search_project_knowledge', {'query': 'violet delimiter'})
            agent = knowledge.service(self.store).search(self.a, 'violet delimiter')
            self.assertEqual(mcp['results'], agent['results'])
            source = mcp['results'][0]
            via_uri = mcp_knowledge.read_uri(self.a, source['uri'] + '?offset=4&limit=50')
            via_tool = mcp_knowledge.call(self.a, 'get_project_knowledge', {'source_id': source['source_id'], 'offset': 4, 'limit': 50})
        self.assertEqual(via_uri, via_tool)


if __name__ == '__main__':
    unittest.main()
