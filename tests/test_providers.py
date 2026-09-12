import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import provider_api
import providers


SECRET = "sk-unit-test-secret-never-real"
OTHER_SECRET = "sk-other-test-secret-never-real"


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "provider_settings.json"
        self.settings = providers.ProviderSettings(self.path)
        self.vault = {}
        self.backend = Mock(priority=1)
        self.backend.get_password.side_effect = lambda service, provider: self.vault.get(provider)
        self.backend.set_password.side_effect = lambda service, provider, key: self.vault.__setitem__(provider, key)
        self.backend.delete_password.side_effect = lambda service, provider: self.vault.pop(provider, None)
        self.keyring = Mock()
        self.keyring.get_keyring.return_value = self.backend
        for patcher in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(providers, "keyring", self.keyring),
            patch.object(provider_api, "settings", self.settings),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = FastAPI()
        self.app.include_router(provider_api.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 32100)),
            base_url="http://localhost:8000",
        )
        self.addAsyncCleanup(self.client.aclose)

    async def save(self, provider="openai", **overrides):
        body = {"api_key": SECRET, "storage": "session", "model": "gpt-test"}
        body.update(overrides)
        return await self.client.put(f"/api/providers/{provider}", json=body)

    async def status(self):
        response = await self.client.get("/api/providers")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_secret_free(self, response):
        for secret in (SECRET, OTHER_SECRET):
            self.assertNotIn(secret, response.text)
        if self.path.exists():
            text = self.path.read_text()
            self.assertNotIn(SECRET, text)
            self.assertNotIn(OTHER_SECRET, text)
            self.assertNotIn("api_key", text)

    def upstream(self, responses=None, error=None):
        client = AsyncMock()
        client.get.side_effect = error if error is not None else responses
        factory = Mock()
        factory.return_value.__aenter__ = AsyncMock(return_value=client)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        return patch.object(providers.httpx, "AsyncClient", factory), client, factory

    async def test_empty_exact_shape(self):
        self.assertEqual(await self.status(), {
            "providers": [
                {"id": "openai", "name": "OpenAI", "configured": False, "source": None, "default_model": None},
                {"id": "anthropic", "name": "Anthropic", "configured": False, "source": None, "default_model": None},
            ],
            "keyring_available": True, "default_provider": None, "onboarding_dismissed": False,
        })

    async def test_environment_detection_precedence_and_settings_only(self):
        self.vault["openai"] = OTHER_SECRET
        self.settings._sessions["openai"] = OTHER_SECRET
        with patch.dict(os.environ, {"OPENAI_API_KEY": SECRET, "ANTHROPIC_API_KEY": OTHER_SECRET}):
            status = await self.status()
            self.assertEqual([p["source"] for p in status["providers"]], ["environment", "environment"])
            for storage in ("session", "keyring"):
                response = await self.save(storage=storage)
                self.assertEqual(response.status_code, 409)
                self.assertIn("Unset", response.json()["detail"])
                self.assert_secret_free(response)
            response = await self.client.put("/api/providers/openai", json={"storage": "keyring", "model": "gpt-test"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assert_secret_free(response)
            self.assertEqual(self.vault["openai"], OTHER_SECRET)

    async def test_lifecycle_session_default_skip_and_remove(self):
        response = await self.save()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("API process", response.json()["message"])
        self.assert_secret_free(response)
        await self.save("anthropic", api_key=OTHER_SECRET, model="claude-test", make_default=False)
        status = await self.status()
        self.assertEqual(status["default_provider"], "openai")
        self.assertEqual(status["providers"][0]["source"], "session")
        for dismissed in (True, False, True):
            response = await self.client.post("/api/providers/onboarding", json={"dismissed": dismissed})
            self.assertEqual(response.json(), {"onboarding_dismissed": dismissed})
        fresh = providers.ProviderSettings(self.path).status()
        self.assertTrue(fresh["onboarding_dismissed"])
        self.assertFalse(fresh["providers"][0]["configured"])
        self.assertEqual(fresh["providers"][0]["default_model"], "gpt-test")
        response = await self.client.delete("/api/providers/openai", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 200)
        status = await self.status()
        self.assertIsNone(status["default_provider"])
        self.assertIsNone(status["providers"][0]["default_model"])
        self.assertFalse(status["providers"][0]["configured"])
        self.assert_secret_free(response)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_keyring_lifecycle_and_storage_switch(self):
        self.assertEqual((await self.save(storage="keyring")).status_code, 200)
        self.assertEqual((await self.status())["providers"][0]["source"], "keyring")
        self.assertEqual(providers.ProviderSettings(self.path).resolve("openai"), (SECRET, "keyring"))
        self.assertEqual((await self.save(api_key=OTHER_SECRET)).status_code, 200)
        self.assertNotIn("openai", self.vault)
        self.assertEqual((await self.save(storage="keyring")).status_code, 200)
        self.assertNotIn("openai", self.settings._sessions)
        response = await self.client.put("/api/providers/openai", json={"storage": "session", "model": "gpt-other"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.vault["openai"], SECRET)
        await self.client.delete("/api/providers/openai", headers={"Content-Type": "application/json"})
        self.assertEqual(self.vault, {})
        self.assert_secret_free(response)

    async def test_delete_environment_explanation_and_managed_cleanup(self):
        await self.save()
        self.vault["openai"] = OTHER_SECRET
        with patch.dict(os.environ, {"OPENAI_API_KEY": SECRET}):
            response = await self.client.delete("/api/providers/openai", headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("Environment variables cannot be deleted", response.json()["message"])
            self.assertEqual((await self.status())["providers"][0]["source"], "environment")
            self.assertEqual(os.environ["OPENAI_API_KEY"], SECRET)
            self.assertEqual(self.vault, {})
            self.assertEqual(self.settings._sessions, {})
            self.assert_secret_free(response)

    async def test_keyring_missing_locked_and_no_plaintext_fallback(self):
        with patch.object(providers, "keyring", None):
            self.assertFalse((await self.status())["keyring_available"])
            response = await self.save(storage="keyring")
            self.assertEqual(response.status_code, 503)
            self.assert_secret_free(response)
            self.assertEqual((await self.save()).status_code, 200)
            response = await self.client.delete("/api/providers/openai", headers={"Content-Type": "application/json"})
            self.assertIn("could not be removed", response.json()["message"])
        self.backend.get_password.side_effect = RuntimeError(SECRET)
        self.assertFalse((await self.status())["keyring_available"])
        response = await self.client.post("/api/providers/openai/test", json={})
        self.assertEqual(response.status_code, 503)
        self.assert_secret_free(response)
        self.backend.set_password.side_effect = RuntimeError(SECRET)
        response = await self.save(storage="keyring")
        self.assertEqual(response.status_code, 503)
        self.assert_secret_free(response)
        for module in ("keyrings.alt.file", "keyring.backends.chainer"):
            unsafe = type("UnsafeBackend", (), {"priority": 1, "__module__": module})()
            self.keyring.get_keyring.return_value = unsafe
            self.assertFalse((await self.status())["keyring_available"])

    async def test_unavailable_keyring_session_switch_warns_about_persisted_key(self):
        self.assertEqual((await self.save(storage="keyring")).status_code, 200)
        with patch.object(providers, "keyring", None):
            response = await self.save(api_key=OTHER_SECRET)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("not a confirmed session-only switch", response.json()["message"])
            self.assertIn("older credential may become active again after restart", response.json()["message"])
            self.assertEqual(self.settings.resolve("openai"), (OTHER_SECRET, "session"))
            self.assertEqual(self.vault["openai"], SECRET)
            self.assert_secret_free(response)
        fresh = providers.ProviderSettings(self.path)
        self.assertEqual(fresh.resolve("openai"), (SECRET, "keyring"))
        response = await self.save(api_key=OTHER_SECRET)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Warning", response.json()["message"])
        self.assertEqual(self.vault, {})
        self.assertEqual(fresh.resolve("openai"), (None, None))

    async def test_failed_preference_commit_preserves_credentials_and_settings(self):
        for initial in ("session", "keyring"):
            for operation in ("session", "keyring", "settings-only", "delete", "unavailable-session"):
                with self.subTest(initial=initial, operation=operation):
                    self.vault.clear()
                    self.settings._sessions.clear()
                    self.assertEqual((await self.save(storage=initial)).status_code, 200)
                    previous_json = self.path.read_bytes()
                    previous_vault = self.vault.copy()
                    previous_sessions = self.settings._sessions.copy()
                    written_json = []

                    def fail_replace(source, destination):
                        # Inspect the actual temporary JSON, including on rollback paths.
                        written_json.append(Path(source).read_text())
                        raise OSError(SECRET)

                    with patch.object(providers.os, "replace", side_effect=fail_replace) as replace:
                        if operation == "delete":
                            response = await self.client.delete("/api/providers/openai", headers={"Content-Type": "application/json"})
                        elif operation == "settings-only":
                            response = await self.save(api_key=None, model="gpt-other")
                        elif operation == "unavailable-session":
                            with patch.object(providers, "keyring", None):
                                response = await self.save(api_key=OTHER_SECRET, model="gpt-other")
                        else:
                            response = await self.save(storage=operation, api_key=OTHER_SECRET, model="gpt-other")
                    self.assertEqual(response.status_code, 503, response.text)
                    replace.assert_called_once()
                    self.assertEqual(len(written_json), 1)
                    self.assertNotIn(SECRET, written_json[0])
                    self.assertNotIn(OTHER_SECRET, written_json[0])
                    self.assertNotIn("api_key", written_json[0])
                    self.assertEqual(self.vault, previous_vault)
                    self.assertEqual(self.settings._sessions, previous_sessions)
                    self.assertEqual(self.path.read_bytes(), previous_json)
                    self.assertEqual(self.settings.resolve("openai"), (SECRET, initial))
                    self.assert_secret_free(response)
                    self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["provider_settings.json", "provider_settings.lock"])

    async def test_partially_failed_keyring_mutations_are_rolled_back(self):
        for operation in ("session", "keyring", "delete"):
            with self.subTest(operation=operation):
                self.assertEqual((await self.save(storage="keyring")).status_code, 200)
                previous_json = self.path.read_bytes()

                def failing_set(service, provider, credential):
                    self.vault[provider] = credential
                    if credential == OTHER_SECRET:
                        raise RuntimeError(OTHER_SECRET)

                def failing_delete(service, provider):
                    self.vault.pop(provider, None)
                    raise RuntimeError(SECRET)

                with patch.object(self.backend, "set_password", side_effect=failing_set), patch.object(self.backend, "delete_password", side_effect=failing_delete):
                    if operation == "delete":
                        response = await self.client.delete("/api/providers/openai", headers={"Content-Type": "application/json"})
                    else:
                        response = await self.save(storage=operation, api_key=OTHER_SECRET, model="gpt-other")
                self.assertEqual(response.status_code, 503, response.text)
                self.assertIn("preserved", response.json()["detail"])
                self.assertEqual(self.vault, {"openai": SECRET})
                self.assertEqual(self.settings._sessions, {})
                self.assertEqual(self.path.read_bytes(), previous_json)
                self.assert_secret_free(response)

    async def test_keyring_rollback_failure_reports_uncertainty_without_committing_session(self):
        self.assertEqual((await self.save()).status_code, 200)
        previous_json = self.path.read_bytes()
        with patch.object(providers.os, "replace", side_effect=OSError(SECRET)), patch.object(self.backend, "delete_password", side_effect=RuntimeError(OTHER_SECRET)):
            response = await self.save(storage="keyring", api_key=OTHER_SECRET, model="gpt-other")
        self.assertEqual(response.status_code, 503)
        self.assertIn("keyring rollback failed", response.json()["detail"])
        self.assertIn("state is uncertain", response.json()["detail"])
        self.assertEqual(self.settings._sessions, {"openai": SECRET})
        self.assertEqual(self.vault, {"openai": OTHER_SECRET})
        self.assertEqual(self.path.read_bytes(), previous_json)
        self.assert_secret_free(response)

    async def test_keyring_read_failure_cannot_change_existing_state(self):
        self.assertEqual((await self.save(storage="keyring")).status_code, 200)
        previous_json = self.path.read_bytes()
        with patch.object(self.backend, "get_password", side_effect=RuntimeError(SECRET)):
            for storage in ("session", "keyring"):
                response = await self.save(storage=storage, api_key=OTHER_SECRET, model="gpt-other")
                self.assertEqual(response.status_code, 503)
                self.assertIn("No changes were saved", response.json()["detail"])
                self.assertEqual(self.vault, {"openai": SECRET})
                self.assertEqual(self.settings._sessions, {})
                self.assertEqual(self.path.read_bytes(), previous_json)
                self.assert_secret_free(response)

    async def test_invalid_requests_never_echo_secrets(self):
        bodies = [
            {"api_key": {"secret": SECRET}},
            {"api_key": [SECRET]},
            {"api_key": SECRET, "storage": SECRET},
            {"api_key": SECRET, "model": {"value": SECRET}},
            {"api_key": SECRET, SECRET: SECRET},
            {"api_key": SECRET, "make_default": SECRET},
            {"api_key": " " + SECRET},
            {"api_key": "x" * 4097},
            {"api_key": ""},
            {"model": ""}, {"model": "x" * 201}, {"model": "bad model"},
        ]
        for overrides in bodies:
            with self.subTest(overrides=list(overrides)):
                response = await self.save(**overrides)
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_secret_free(response)
        for body in ('{"api_key":"' + SECRET, json.dumps([SECRET])):
            response = await self.client.post("/api/providers/openai/test", content=body, headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 422)
            self.assert_secret_free(response)
        response = await self.save(model=SECRET)
        self.assertEqual(response.status_code, 422)
        self.assert_secret_free(response)
        response = await self.client.post("/api/providers/onboarding", json={"dismissed": SECRET})
        self.assertEqual(response.status_code, 422)
        self.assert_secret_free(response)
        self.assertEqual((await self.save(api_key=None)).status_code, 422)

    async def test_openai_listing_provided_key_does_not_save_or_verify_model(self):
        upstream, client, factory = self.upstream([
            httpx.Response(200, json={"data": [{"id": "gpt-test"}, {"id": SECRET}, {"id": "safe", "display_name": SECRET}]})
        ])
        with patch.dict(os.environ, {"OPENAI_API_KEY": OTHER_SECRET}), upstream:
            response = await self.client.post("/api/providers/openai/test", json={"api_key": SECRET, "model": "not-in-list"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["models"], [{"id": "gpt-test", "name": "gpt-test"}, {"id": "safe", "name": "safe"}])
        self.assertIn("does not verify inference", response.json()["message"])
        self.assertEqual(client.get.call_args.args, ("https://api.openai.com/v1/models",))
        self.assertEqual(client.get.call_args.kwargs["headers"], {"Authorization": f"Bearer {SECRET}"})
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])
        self.assertLessEqual(factory.call_args.kwargs["timeout"].read, 10)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.vault, {})
        self.assert_secret_free(response)

    async def test_listing_resolves_existing_key_and_anthropic_paginates(self):
        await self.save("anthropic", model="claude-test")
        upstream, client, _ = self.upstream([
            httpx.Response(200, json={"data": [{"id": "claude-first", "display_name": "Claude First"}], "has_more": True, "last_id": "claude-first"}),
            httpx.Response(200, json={"data": [{"id": "claude-next"}], "has_more": False}),
        ])
        with upstream:
            response = await self.client.post("/api/providers/anthropic/test", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["models"]), 2)
        self.assertEqual(response.json()["models"][0]["name"], "Claude First")
        self.assertEqual(client.get.call_args.args, ("https://api.anthropic.com/v1/models",))
        self.assertEqual(client.get.call_args.kwargs["params"], {"limit": 100, "after_id": "claude-first"})
        self.assertEqual(client.get.call_args.kwargs["headers"]["x-api-key"], SECRET)
        self.assertEqual(client.get.call_args.kwargs["headers"]["anthropic-version"], "2023-06-01")
        self.assert_secret_free(response)

    async def test_pagination_is_bounded(self):
        pages = [httpx.Response(200, json={"data": [{"id": f"claude-{i}"}], "has_more": True, "last_id": f"claude-{i}"}) for i in range(providers.MAX_MODEL_PAGES)]
        upstream, client, _ = self.upstream(pages)
        with upstream:
            response = await self.client.post("/api/providers/anthropic/test", json={"api_key": SECRET})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get.call_count, providers.MAX_MODEL_PAGES)
        self.assertIn("truncated", response.json()["message"])

    async def test_auth_network_and_bad_upstream_errors_sanitized(self):
        for status, expected in ((401, 401), (403, 401), (429, 429), (500, 502), (302, 502)):
            upstream, _, _ = self.upstream([httpx.Response(status, text=SECRET)])
            with upstream:
                response = await self.client.post("/api/providers/openai/test", json={"api_key": SECRET})
            self.assertEqual(response.status_code, expected)
            self.assert_secret_free(response)
        for error, expected in ((httpx.ConnectError(SECRET), 502), (httpx.ReadTimeout(SECRET), 504)):
            upstream, _, _ = self.upstream(error=error)
            with upstream:
                response = await self.client.post("/api/providers/openai/test", json={"api_key": SECRET})
            self.assertEqual(response.status_code, expected)
            self.assert_secret_free(response)
        for payload in (SECRET, {"data": SECRET}, {"data": [SECRET]}, {"data": [], "has_more": True, "last_id": SECRET + " "}):
            upstream, _, _ = self.upstream([httpx.Response(200, json=payload)])
            with upstream:
                response = await self.client.post("/api/providers/anthropic/test", json={"api_key": SECRET})
            self.assertEqual(response.status_code, 502)
            self.assert_secret_free(response)
        self.assertEqual((await self.client.post("/api/providers/openai/test", json={})).status_code, 422)

    async def test_save_does_not_call_provider(self):
        with patch.object(providers.httpx, "AsyncClient", side_effect=AssertionError("Unexpected upstream call")):
            self.assertEqual((await self.save()).status_code, 200)

    async def test_listing_classifies_429_without_exposing_upstream_text(self):
        for provider, error, expected in (
            ("openai", {"code": "insufficient_quota", "message": SECRET}, "API quota/billing blocked"),
            ("openai", {"type": "rate_limit_exceeded", "message": SECRET}, "at least 7 seconds"),
            ("anthropic", {"type": "rate_limit_error", "message": SECRET}, "at least 7 seconds"),
            ("openai", {"code": [SECRET], "type": SECRET}, "without a recognized reason"),
        ):
            with self.subTest(provider=provider, expected=expected):
                upstream, client, _ = self.upstream([httpx.Response(429, json={"error": error}, headers={"retry-after": "7"})])
                with upstream:
                    response = await self.client.post(f"/api/providers/{provider}/test", json={"api_key": SECRET})
                self.assertEqual(response.status_code, 429)
                self.assertIn(expected, response.json()["detail"])
                self.assert_secret_free(response)
                self.assertEqual(client.get.call_count, 1)

    def test_limit_diagnostic_unknown_payloads_and_retry_headers_are_safe(self):
        for raw in (b"not json", b"[]", b"null", b'{"error": null}', b"x" * (1024 * 1024 + 1)):
            self.assertIn("without a recognized reason", providers.limit_error_message("openai", raw))
        raw = b'{"error":{"code":"rate_limit_exceeded"}}'
        for header in (SECRET, "-1", "9999999999999", "86401", "0"):
            result = providers.limit_error_message("openai", raw, header)
            self.assertNotIn("at least", result)
            self.assertNotIn(SECRET, result)

    async def test_total_listing_deadline(self):
        upstream, client, _ = self.upstream()

        async def slow_response(*args, **kwargs):
            await asyncio.sleep(1)
            return httpx.Response(200, json={"data": []})

        client.get.side_effect = slow_response
        with upstream, patch.object(providers, "MODEL_LIST_TIMEOUT", 0.001):
            response = await self.client.post("/api/providers/openai/test", json={"api_key": SECRET})
        self.assertEqual(response.status_code, 504)
        self.assert_secret_free(response)

    async def test_router_in_real_app_before_spa_preserves_existing_routes(self):
        import api

        paths = [route.path for route in api.app.routes]
        self.assertLess(paths.index("/api/providers"), paths.index("/{full_path:path}"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, client=("127.0.0.1", 12)), base_url="http://localhost:8000") as client:
            with patch.object(api.storage, "_load", side_effect=AssertionError("Real data access")):
                self.assertEqual((await client.get("/api/providers")).status_code, 200)
                with patch.object(api.storage, "list_stores", return_value=[]):
                    response = await client.get("/api/stores")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), [])
                response = await client.put("/api/providers/openai", json={"api_key": {"secret": SECRET}})
                self.assertEqual(response.status_code, 422)
                self.assert_secret_free(response)

    async def test_invalid_provider(self):
        for provider in ("unknown", "OpenAI", "x" * 300):
            for method, suffix, body in (("PUT", "", {"storage": "session", "model": "gpt-test", "api_key": SECRET}), ("POST", "/test", {"api_key": SECRET}), ("DELETE", "", None)):
                response = await self.client.request(method, f"/api/providers/{provider}{suffix}", json=body, headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 404)
                self.assert_secret_free(response)

    async def test_origin_host_forwarded_guards_all_endpoints(self):
        endpoints = [("GET", ""), ("POST", "/openai/test"), ("PUT", "/openai"), ("DELETE", "/openai"), ("POST", "/onboarding")]
        bad_headers = [
            {"Origin": "http://evil.example"}, {"Origin": "null"},
            {"Origin": "http://localhost"}, {"Origin": "https://localhost:8000"},
            {"Origin": "http://127.0.0.1:8000"}, {"Origin": "http://localhost:8000/"},
            {"Host": "evil.example"}, {"Host": "localhost.evil.example"},
            {"Host": "127.0.0.1@evil.example"}, {"Host": "localhost:99999"},
            {"Host": "127.999.0.1"}, {"Host": "192.168.1.1"},
            {"X-Forwarded-For": "127.0.0.1"}, {"X-Forwarded-Host": "localhost:8000"},
            {"X-Forwarded-Proto": "http"}, {"Forwarded": "for=127.0.0.1"}, {"X-Real-IP": "127.0.0.1"},
        ]
        for headers in bad_headers:
            for method, suffix in endpoints:
                response = await self.client.request(method, "/api/providers" + suffix, headers=headers, content=SECRET)
                self.assertEqual(response.status_code, 403, (headers, method, response.text))
                self.assert_secret_free(response)
        self.assertFalse(self.path.exists())
        for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
            response = await self.client.get("/api/providers", headers={"Host": host, "Origin": "http://" + host})
            self.assertEqual(response.status_code, 200)
        for header in ("host", "origin"):
            response = await self.client.get("/api/providers", headers=[(header, "localhost:8000"), (header, "localhost:8000")])
            self.assertEqual(response.status_code, 403)

    async def test_remote_client_cannot_spoof_local(self):
        for peer in ("192.168.1.10", "8.8.8.8", "testclient"):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=(peer, 12)), base_url="http://localhost:8000") as client:
                response = await client.get("/api/providers")
                self.assertEqual(response.status_code, 403)
                response = await client.get("/api/providers", headers={"X-Forwarded-For": "127.0.0.1"})
                self.assertEqual(response.status_code, 403)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=("::1", 12)), base_url="http://[::1]:8000") as client:
            self.assertEqual((await client.get("/api/providers")).status_code, 200)

    async def test_writes_require_json_and_body_is_bounded(self):
        for content_type in (None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            for method, suffix in (("PUT", "/openai"), ("POST", "/openai/test"), ("POST", "/onboarding"), ("DELETE", "/openai")):
                headers = {"Content-Type": content_type} if content_type else {}
                response = await self.client.request(method, "/api/providers" + suffix, content=SECRET, headers=headers)
                self.assertEqual(response.status_code, 415)
                self.assert_secret_free(response)
        response = await self.save(api_key="x" * 17000)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.path.exists())

    async def test_preference_errors_and_atomic_write_failure(self):
        self.path.write_text(json.dumps({"default_provider": [SECRET]}))
        response = await self.client.get("/api/providers")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(SECRET, response.text)
        self.path.unlink()
        await self.save()
        previous = self.path.read_bytes()
        with patch.object(providers.os, "replace", side_effect=OSError(SECRET)):
            response = await self.client.post("/api/providers/onboarding", json={"dismissed": True})
        self.assertEqual(response.status_code, 503)
        self.assert_secret_free(response)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["provider_settings.json", "provider_settings.lock"])

    async def test_concurrent_preference_updates_preserve_fields(self):
        stores = [providers.ProviderSettings(self.path) for _ in range(3)]
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(stores[0].save, "openai", SECRET, "session", "gpt-test", True),
                executor.submit(stores[1].save, "anthropic", OTHER_SECRET, "session", "claude-test", False),
                executor.submit(stores[2].dismiss, True),
            ]
            for future in futures:
                future.result()
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw, {"models": {"openai": "gpt-test", "anthropic": "claude-test"}, "default_provider": "openai", "onboarding_dismissed": True})


if __name__ == "__main__":
    unittest.main()
