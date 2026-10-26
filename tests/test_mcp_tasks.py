import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import mcp_tasks
import mcp_server


class MCPTasksTests(unittest.IsolatedAsyncioTestCase):
    async def test_continue_tool_keeps_task_id_and_forwards_idempotency_key(self):
        store_id, run_id, message_id, turn_id = (str(uuid4()) for _ in range(4))
        body = {"run_id": run_id, "goal": "Add tests to that code", "provider": "openai", "mode": "direct",
                "message_id": message_id, "expected_turn_id": turn_id}
        def handle(request):
            self.assertEqual(request.url.path, f"/api/stores/{store_id}/task-runs/{run_id}/messages")
            sent = json.loads(request.content)
            self.assertEqual(sent["message_id"], message_id)
            self.assertNotIn("run_id", sent)
            return httpx.Response(202, json={"id": run_id, "turn_id": message_id})
        client = httpx.AsyncClient(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(handle))
        with patch.object(mcp_tasks.httpx, "AsyncClient", return_value=client):
            result = await mcp_tasks.call_task_tool(store_id, "continue_task_run", body)
        self.assertFalse(result.isError)
        self.assertEqual(json.loads(result.content[0].text)["id"], run_id)

    async def test_registered_tools_and_bridge_uses_project_api(self):
        store_id = str(uuid4())
        server = mcp_server.make_mcp_server(store_id, 'Example')
        catalog = await server.request_handlers[ListToolsRequest](ListToolsRequest(method='tools/list'))
        names = {tool.name for tool in catalog.root.tools}
        self.assertTrue(mcp_tasks.NAMES <= names)
        self.assertNotIn('spawn_subagent', names)
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(202, json={'id': str(uuid4()), 'status': 'queued'})
        client = httpx.AsyncClient(base_url='http://127.0.0.1:8000', transport=httpx.MockTransport(handle))
        with patch.object(mcp_tasks.httpx, 'AsyncClient', return_value=client):
            result = await server.request_handlers[CallToolRequest](CallToolRequest(method='tools/call', params=CallToolRequestParams(
                name='start_task_run', arguments={'goal': 'Draft a plan', 'provider': 'auto', 'schema_retries': 1})))
        self.assertFalse(result.root.isError)
        self.assertEqual(str(calls[0].url), f'http://127.0.0.1:8000/api/stores/{store_id}/task-runs')
        self.assertEqual(json.loads(calls[0].content)['schema_retries'], 1)

    async def test_invalid_ids_and_secret_fields_never_forward(self):
        with patch.object(mcp_tasks.httpx, 'AsyncClient') as client:
            for name, body in [('get_task_run', {'run_id': '../other'}),
                               ('start_task_run', {'goal': 'Draft', 'provider': 'openai', 'api_key': 'secret'})]:
                result = await mcp_tasks.call_task_tool(str(uuid4()), name, body)
                self.assertTrue(result.isError)
                self.assertNotIn('secret', result.content[0].text)
            client.assert_not_called()

    async def test_cancel_and_api_failure(self):
        store_id, run_id = str(uuid4()), str(uuid4())
        def handle(request):
            self.assertEqual(request.method, 'POST')
            self.assertTrue(request.url.path.endswith(f'/{run_id}/cancel'))
            self.assertEqual(json.loads(request.content), {})
            return httpx.Response(404, json={'detail': 'Task run not found in this store.'})
        client = httpx.AsyncClient(base_url='http://127.0.0.1:8000', transport=httpx.MockTransport(handle))
        with patch.object(mcp_tasks.httpx, 'AsyncClient', return_value=client):
            result = await mcp_tasks.call_task_tool(store_id, 'cancel_task_run', {'run_id': run_id})
        self.assertTrue(result.isError)
        self.assertIn('not found', result.content[0].text)

    async def test_mcp_ingress_rejects_remote_and_cross_origin(self):
        for peer, headers in [(('192.0.2.1', 1234), {}),
                              (('127.0.0.1', 1234), {'Origin': 'https://example.org'}),
                              (('127.0.0.1', 1234), {'Host': 'evil.example'})]:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_server.asgi_app, client=peer), base_url='http://localhost:8001') as client:
                self.assertEqual((await client.get('/health', headers=headers)).status_code, 403)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_server.asgi_app), base_url='http://localhost:8001') as client:
            self.assertEqual((await client.get('/health')).status_code, 200)
