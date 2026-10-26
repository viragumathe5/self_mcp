"""Protocol checks against the real ASGI transports; no personal storage or model calls."""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import api
import mcp_server

PROJECT = {'id': 'a7c81063-d071-43d2-9933-e9c845860001', 'slug': 'example', 'name': 'Example'}


class MCPTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lookup = patch.object(mcp_server.storage, 'get_store_by_slug', side_effect=lambda slug: PROJECT if slug == 'example' else None)
        self.lookup.start()
        self.addCleanup(self.lookup.stop)

    async def rpc(self, client, method, params=None, path='/mcp/example/mcp'):
        return await client.post(path, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}},
                                 headers={'Accept': 'application/json, text/event-stream'})

    async def test_shared_app_handshake_catalog_and_project_read(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://127.0.0.1:8000') as client:
            response = await self.rpc(client, 'initialize', {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['result']['serverInfo']['name'], 'self_mcp/Example')
            response = await self.rpc(client, 'tools/list')
            self.assertEqual(response.status_code, 200)
            self.assertIn('continue_task_run', {t['name'] for t in response.json()['result']['tools']})
            with patch.object(mcp_server.storage, 'list_links', return_value=[{'title': 'Saved reference', 'url': 'https://example.com'}]) as read:
                response = await self.rpc(client, 'tools/call', {'name': 'list_links', 'arguments': {}})
                self.assertIn('Saved reference', response.json()['result']['content'][0]['text'])
                read.assert_called_once_with(PROJECT['id'])
            self.assertEqual((await client.get('/mcp/health')).text, 'ok')
            self.assertIn('text/html', (await client.get('/')).headers['content-type'])

    async def test_unknown_project_and_bad_origin(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://127.0.0.1:8000') as client:
            self.assertEqual((await self.rpc(client, 'tools/list', path='/mcp/missing/mcp')).status_code, 404)
            self.assertEqual((await client.post('/mcp/example/mcp', json={}, headers={'Origin': 'https://evil.example'})).status_code, 403)
            self.assertEqual((await client.post('/mcp/example/mcp', content='{}', headers={'Content-Type': 'text/plain'})).status_code, 415)
            self.assertEqual((await client.get('/mcp/example/sse/extra')).status_code, 404)

    async def test_discovery_urls_follow_mount_and_port(self):
        with patch.object(mcp_server.storage, 'list_stores', return_value=[PROJECT]):
            for app, base, prefix in [(api.app, 'http://127.0.0.1:8000', '/mcp'), (mcp_server.asgi_app, 'http://localhost:8001', '')]:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base) as client:
                    response = await client.get(prefix + '/stores')
                    item = response.json()[0]
                    self.assertEqual(item['url'], base + prefix + '/example/mcp')
                    self.assertEqual(item['sse_url'], base + prefix + '/example/sse')

    async def test_mounted_legacy_sse_announces_working_post_endpoint(self):
        queue = asyncio.Queue()
        await queue.put({'type': 'http.request', 'body': b'', 'more_body': False})
        endpoint = asyncio.Future()
        initialized = asyncio.Event()
        async def send(event):
            body = event.get('body', b'').decode()
            for line in body.splitlines():
                if line.startswith('data: /') and not endpoint.done():
                    endpoint.set_result(line.removeprefix('data: '))
            if 'serverInfo' in body:
                initialized.set()
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'scheme': 'http',
                 'method': 'GET', 'path': '/mcp/example/sse', 'raw_path': b'/mcp/example/sse', 'query_string': b'',
                 'root_path': '', 'headers': [(b'host', b'127.0.0.1:8000')], 'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 8000)}
        task = asyncio.create_task(api.app(scope, queue.get, send))
        try:
            uri = await asyncio.wait_for(endpoint, 3)
            self.assertEqual(urlsplit(uri).path, '/mcp/example/messages/')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://127.0.0.1:8000') as client:
                response = await self.rpc(client, 'initialize', {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}, path=uri)
                self.assertEqual(response.status_code, 202)
                await asyncio.wait_for(initialized.wait(), 3)
        finally:
            await queue.put({'type': 'http.disconnect'})
            await asyncio.wait_for(task, 3)
