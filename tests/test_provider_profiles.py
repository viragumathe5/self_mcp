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
import provider_transport
import providers


SECRET = "sk-profile-unit-secret-never-real"
OTHER = "sk-second-unit-secret-never-real"
HTTP_CLIENT = httpx.AsyncClient
PROFILE = {"name": "Local Ollama", "kind": "ollama", "base_url": "http://localhost:11434/",
           "storage": "keyring", "model": ""}


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "provider_settings.json"
        self.settings = providers.ProviderSettings(self.path)
        self.vault = {}
        self.backend = Mock(priority=1)
        self.backend.get_password.side_effect = lambda service, identifier: self.vault.get(identifier)
        self.backend.set_password.side_effect = lambda service, identifier, key: self.vault.__setitem__(identifier, key)
        self.backend.delete_password.side_effect = lambda service, identifier: self.vault.pop(identifier, None)
        keyring = Mock()
        keyring.get_keyring.return_value = self.backend
        for patcher in (patch.dict(os.environ, {}, clear=True), patch.object(providers, "keyring", keyring),
                        patch.object(provider_api, "settings", self.settings)):
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(provider_api.router)
        self.client = HTTP_CLIENT(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)), base_url="http://localhost:8000")
        self.addAsyncCleanup(self.client.aclose)

    async def create(self, **fields):
        response = await self.client.post("/api/providers/profiles", json={**PROFILE, **fields})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {"id", "message"})
        self.assertRegex(response.json()["id"], r"^profile_[0-9a-f]{32}$")
        return response.json()["id"]

    async def update(self, identifier, **fields):
        return await self.client.put(f"/api/providers/profiles/{identifier}", json={**PROFILE, **fields})

    async def delete(self, identifier):
        return await self.client.delete(f"/api/providers/{identifier}", headers={"Content-Type": "application/json"})

    def upstream(self, payload=None, status=200, error=None, handler=None):
        requests = []

        async def respond(request):
            requests.append(request)
            if error:
                raise error
            if handler:
                return await handler(request)
            return httpx.Response(status, json=payload)

        factory = Mock(side_effect=lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(respond), **kw))
        return patch.object(provider_transport.httpx, "AsyncClient", factory), requests, factory

    def secret_free(self, response):
        text = response.text if isinstance(response, httpx.Response) else json.dumps(response)
        for secret in (SECRET, OTHER):
            self.assertNotIn(secret, text)
            if self.path.exists():
                self.assertNotIn(secret, self.path.read_text())
        if self.path.exists():
            self.assertNotIn("api_key", self.path.read_text())

    async def test_keyless_ollama_shape_connection_and_no_keyring_prompt(self):
        with patch.object(providers, "keyring", None):
            identifier = await self.create()
            connection = self.settings.connection(identifier)
        self.backend.get_password.assert_not_called()
        self.assertEqual(connection, {"id": identifier, "kind": "ollama", "base_url": "http://localhost:11434",
                                     "is_local": True, "api_key": None, "context_tokens": 8192,
                                     "output_tokens": 2048, "requires_key": False, "allowed_models": []})
        self.assertEqual(self.settings.resolve(identifier), (None, "none"))
        self.assertEqual(self.settings.routing(), {"mode": "local_only", "providers": []})
        status = self.settings.status()
        self.assertEqual([p["id"] for p in status["providers"]], ["openai", "anthropic", identifier])
        self.assertEqual(status["providers"][-1], {"id": identifier, "name": "Local Ollama", "configured": True,
                         "source": "none", "default_model": "", "kind": "ollama", "base_url": "http://localhost:11434",
                          "is_local": True, "allow_routing": False, "requires_key": False,
                          "context_tokens": 8192, "output_tokens": 2048, "allowed_models": []})
        self.assertEqual(providers.ProviderSettings(self.path).connection(identifier), connection)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.secret_free(status)

    async def test_profiles_preferences_and_explicit_routing_survive_builtin_saves(self):
        with patch.object(provider_transport.httpx, "AsyncClient", side_effect=AssertionError("Unexpected network")):
            first = await self.create(make_default=True)
            second = await self.create(name="Remote A", kind="openai_compatible", base_url="https://models.example/v1", model="custom/model")
            third = await self.create(name="Remote B", kind="openai_compatible", base_url="https://other.example/v1", api_key=SECRET)
            self.assertEqual(self.settings.routing()["providers"], [])
            routing = {"mode": "local_first", "providers": [first, second, "openai"]}
            response = await self.client.put("/api/providers/routing", json=routing)
            self.assertEqual(response.json(), routing)
            self.settings.save("openai", OTHER, "session", "gpt-original", False)
            self.settings.dismiss(True)
            response = await self.update(first, name="Renamed", model="llama3:latest")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["id"], first)
            fresh = providers.ProviderSettings(self.path)
            status = fresh.status()
            self.assertEqual(status["default_provider"], first)
            self.assertTrue(status["onboarding_dismissed"])
            self.assertEqual(status["routing"], routing)
            self.assertEqual([p["allow_routing"] for p in status["providers"]], [True, False, True, True, False])
            self.assertEqual(status["providers"][2]["default_model"], "llama3:latest")
            self.assertEqual((await self.client.get("/api/providers/routing")).json(), routing)
            self.assertEqual((await self.delete(first)).status_code, 200)
            self.assertIsNone(self.settings.status()["default_provider"])
            self.assertEqual(self.settings.routing()["providers"], [second, "openai"])
            self.assertEqual((await self.delete(third)).status_code, 200)
            self.assertNotIn(third, self.vault)
            self.assertEqual((await self.delete("openai")).status_code, 200)
            self.assertEqual(self.settings.routing()["providers"], [second])
        self.secret_free(status)

    async def test_session_restart_missing_key_never_falls_back_to_environment_or_keyring(self):
        identifier = await self.create(api_key=SECRET, storage="session")
        self.assertTrue(self.settings.connection(identifier)["requires_key"])
        self.vault[identifier] = OTHER
        with patch.dict(os.environ, {"OPENAI_API_KEY": OTHER}):
            fresh = providers.ProviderSettings(self.path)
            self.assertEqual(fresh.resolve(identifier), (None, "none"))
            self.assertFalse(fresh.status()["providers"][-1]["configured"])
            with self.assertRaises(providers.ProviderError) as error:
                fresh.connection(identifier)
            self.assertEqual(error.exception.status, 422)
            with patch.object(provider_api, "settings", fresh):
                response = await self.client.post(f"/api/providers/{identifier}/test", json={})
            self.assertEqual(response.status_code, 422)
            upstream, requests, _ = self.upstream({"models": []})
            with upstream, patch.object(provider_api, "settings", fresh):
                response = await self.client.post(f"/api/providers/{identifier}/test", json={"api_key": SECRET})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(requests[0].headers["authorization"], "Bearer " + SECRET)
            self.assertEqual(fresh.resolve(identifier), (None, "none"))
        self.secret_free(response)

    async def test_blank_key_preserves_and_clear_key_is_explicit(self):
        identifier = await self.create(api_key=SECRET)
        self.assertEqual(providers.ProviderSettings(self.path).resolve(identifier), (SECRET, "keyring"))
        for key in ("", None):
            response = await self.update(identifier, name="New Name", model="qwen:latest", api_key=key, storage="session")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.settings.resolve(identifier), (SECRET, "keyring"))
            self.assertTrue(self.settings.connection(identifier)["requires_key"])
        response = await self.update(identifier, api_key=OTHER, storage="session")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(identifier, self.vault)
        self.assertEqual(self.settings.resolve(identifier), (OTHER, "session"))
        response = await self.update(identifier, clear_api_key=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.settings.resolve(identifier), (None, "none"))
        self.assertFalse(self.settings.connection(identifier)["requires_key"])
        self.secret_free(response)

    async def test_required_keyless_profile_can_be_saved_but_cannot_connect(self):
        identifier = await self.create(requires_key=True)
        self.assertFalse(self.settings.status()["providers"][-1]["configured"])
        with self.assertRaises(providers.ProviderError) as error:
            self.settings.connection(identifier)
        self.assertEqual(error.exception.status, 422)

    async def test_immutable_endpoint_even_with_new_key_and_saved_test_has_no_url(self):
        identifier = await self.create(api_key=SECRET)
        previous = self.path.read_bytes()
        for changes in ({"base_url": "https://other.example/v1"}, {"kind": "openai_compatible"},
                        {"base_url": "http://localhost:11435", "api_key": OTHER}):
            response = await self.update(identifier, **changes)
            self.assertEqual(response.status_code, 422)
            self.assertIn("Recreate", response.json()["detail"])
            self.assertEqual(self.path.read_bytes(), previous)
            self.assertEqual(self.vault[identifier], SECRET)
            self.secret_free(response)
        with patch.object(provider_transport, "discover", side_effect=AssertionError("Unexpected call")):
            response = await self.client.post(f"/api/providers/{identifier}/test", json={"base_url": "https://other.example/v1"})
        self.assertEqual(response.status_code, 422)
        response = await self.update(identifier, base_url="http://LOCALHOST:11434")
        self.assertEqual(response.status_code, 200, response.text)

    async def test_unsaved_discovery_and_key_isolation(self):
        for kind, base, payload, suffix in (
            ("ollama", "http://127.0.0.1:11434", {"models": [{"name": "llama:latest"}]}, "/api/tags"),
            ("openai_compatible", "https://remote.example/prefix/v1/", {"data": [{"id": "chat", "name": "Custom Chat"}]}, "/models"),
        ):
            for key in (None, SECRET):
                upstream, requests, factory = self.upstream(payload)
                with upstream, patch.dict(os.environ, {"OPENAI_API_KEY": OTHER, "ANTHROPIC_API_KEY": OTHER}):
                    response = await self.client.post("/api/providers/profiles/test", json={**PROFILE, "kind": kind, "base_url": base, "api_key": key})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(set(response.json()), {"models", "message"})
                self.assertEqual(str(requests[0].url), base.rstrip("/") + suffix)
                self.assertEqual(requests[0].headers.get("authorization"), "Bearer " + key if key else None)
                self.assertNotIn("x-api-key", requests[0].headers)
                self.assertFalse(factory.call_args.kwargs["trust_env"])
                self.assertFalse(factory.call_args.kwargs["follow_redirects"])
                self.assertEqual(factory.call_args.kwargs["timeout"].read, 90)
                self.assertEqual(len(requests), 1)
                self.assertFalse(self.path.exists())
                self.assertEqual(self.vault, {})
                self.assertEqual(self.settings._sessions, {})
                self.secret_free(response)

    async def test_routing_validation_and_capacity(self):
        for body in ({"mode": "invalid", "providers": []}, {"mode": "quality", "providers": ["unknown"]},
                     {"mode": "balanced", "providers": ["openai", "openai"]},
                     {"mode": "quality", "providers": ["openai"] * 21}):
            response = await self.client.put("/api/providers/routing", json=body)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.settings.routing(), {"mode": "local_only", "providers": []})
        identifiers = [await self.create(name="Duplicate names allowed") for _ in range(18)]
        self.assertEqual(len(set(identifiers)), 18)
        response = await self.client.post("/api/providers/profiles", json=PROFILE)
        self.assertEqual(response.status_code, 422)
        all_ids = ["openai", "anthropic", *identifiers]
        response = await self.client.put("/api/providers/routing", json={"mode": "quality", "providers": all_ids})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.settings.status()["providers"]), 20)

    async def test_invalid_metadata_and_secrets_not_reflected_or_persisted(self):
        for fields in ({"name": "a" * 81}, {"name": "bad\nname"}, {"name": " "}, {"model": "bad model"},
                       {"context_tokens": 2048, "output_tokens": 2048}, {"output_tokens": 0},
                       {"name": SECRET}, {"model": SECRET}, {"base_url": "https://remote.example/" + SECRET},
                       {"base_url": "https://remote.example/" + "%73" + SECRET[1:]},
                       {"api_key": "\n" + SECRET}, {"unknown": SECRET}):
            response = await self.client.post("/api/providers/profiles", json={**PROFILE, "api_key": SECRET, **fields})
            self.assertEqual(response.status_code, 422, response.text)
            self.secret_free(response)
            self.assertFalse(self.path.exists())
        identifier = await self.create(api_key=SECRET)
        for field in ("name", "model"):
            response = await self.update(identifier, **{field: SECRET})
            self.assertEqual(response.status_code, 422)
            self.secret_free(response)
        response = await self.update(identifier, clear_api_key=True, api_key=OTHER)
        self.assertEqual(response.status_code, 422)
        self.secret_free(response)

    async def test_url_guards_and_saved_data_revalidation(self):
        bad_urls = ["ftp://localhost", "file:///tmp/models", "http://remote.example/v1", "http://127.0.0.2:11434",
                    "https://user:password@remote.example/v1", "https://remote.example?key=" + SECRET,
                    "https://remote.example/#fragment", "https://169.254.169.254/latest", "https://0.0.0.0",
                    "https://[::]", "https://[fe80::1]", "https://[::ffff:169.254.169.254]",
                    "https://100.100.100.200", "https://168.63.129.16", "https://[fd00:ec2::254]",
                    "https://metadata.google.internal", "http://2130706433", "http://0x7f000001", "https://127.1",
                    "https://remote.example/%0aevil", "https://remote.example/../v1", "https://remote.example/%2e%2e/v1",
                    "https://remote.example:99999/v1", "https://remote.example\\@localhost/v1", "https://remote.example/v1?"]
        for base in bad_urls:
            with self.subTest(base=base):
                response = await self.client.post("/api/providers/profiles", json={**PROFILE, "base_url": base})
                self.assertEqual(response.status_code, 422, response.text)
                self.secret_free(response)
        for base, expected, local in (("http://[::1]:11434/", "http://[::1]:11434", True),
                                      ("https://REMOTE.example:443//prefix/v1/", "https://remote.example/prefix/v1", False)):
            self.assertEqual(providers.normalize_base_url(base), (expected, local))
        identifier = await self.create()
        raw = json.loads(self.path.read_text())
        raw["profiles"][identifier]["base_url"] = "http://169.254.169.254"
        self.path.write_text(json.dumps(raw))
        with patch.object(provider_transport.httpx, "AsyncClient", side_effect=AssertionError("Unexpected network")):
            response = await self.client.post(f"/api/providers/{identifier}/test", json={})
            self.assertEqual(response.status_code, 503)
            with self.assertRaises(providers.ProviderError) as error:
                await provider_transport.discover({"kind": "ollama", "base_url": "https://169.254.169.254"})
            self.assertEqual(error.exception.status, 422)

    async def test_atomic_profile_rollback_and_unavailable_keyring(self):
        identifier = await self.create(api_key=SECRET)
        previous = self.path.read_bytes()
        for changes in ({"api_key": OTHER}, {"api_key": OTHER, "storage": "session"}, {"clear_api_key": True}, {"name": "Changed"}):
            with patch.object(providers.os, "replace", side_effect=OSError(SECRET)):
                response = await self.update(identifier, **changes)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(self.path.read_bytes(), previous)
            self.assertEqual(self.vault, {identifier: SECRET})
            self.assertEqual(self.settings._sessions, {})
            self.secret_free(response)
        with patch.object(providers.os, "replace", side_effect=OSError(SECRET)):
            response = await self.delete(identifier)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.vault, {identifier: SECRET})
        self.assertEqual(self.path.read_bytes(), previous)
        with patch.object(providers, "keyring", None):
            self.assertEqual((await self.update(identifier, api_key=OTHER, storage="session")).status_code, 503)
            self.assertEqual((await self.delete(identifier)).status_code, 503)
            keyless = await self.create()
            self.assertEqual((await self.delete(keyless)).status_code, 200)
        self.assertEqual((await self.delete(identifier)).status_code, 200)
        self.assertEqual(self.vault, {})

    async def test_concurrent_profile_creation_preserves_preferences(self):
        stores = [providers.ProviderSettings(self.path) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(store.save_profile, **PROFILE) for store in stores]
            ids = [future.result()["id"] for future in futures]
        self.assertEqual(set(self.settings._load()["profiles"]), set(ids))
        self.assertEqual(self.settings.routing(), {"mode": "local_only", "providers": []})

    async def test_builtin_connection_urls_and_model_semantics(self):
        for provider in ("openai", "anthropic"):
            with self.assertRaises(providers.ProviderError) as error:
                self.settings.connection(provider)
            self.assertEqual(error.exception.status, 422)
            self.settings.save(provider, SECRET, "session", "original-model", True)
            connection = self.settings.connection(provider)
            self.assertEqual(connection, {"id": provider, "kind": provider, "base_url": f"https://api.{provider}.com/v1",
                                         "is_local": False, "api_key": SECRET, "context_tokens": 8192, "output_tokens": 2048,
                                         "requires_key": True, "allowed_models": []})
            self.assertEqual(self.settings.status()["default_provider"], provider)
        self.assertEqual([p["default_model"] for p in self.settings.status()["providers"]], ["original-model"] * 2)

    async def test_discovery_redacts_secret_model_metadata(self):
        for kind, payload in (("openai_compatible", {"data": [{"id": SECRET}, {"id": "safe", "name": SECRET}, {"id": "fine", "display_name": "Fine"}]}),
                              ("ollama", {"models": [{"name": SECRET}, {"model": "safe", "name": SECRET}]})):
            upstream, _, _ = self.upstream(payload)
            with upstream:
                result = await provider_transport.discover({"kind": kind, "base_url": "http://localhost:8002/v1", "api_key": SECRET})
            self.assertEqual(result["models"][0], {"id": "safe", "name": "safe", "is_local": True, "remote": False})
            self.secret_free(result)

    async def test_inference_protocol_bodies_and_usage(self):
        for kind, reply, path in (
            ("ollama", {"done": True, "message": {"content": "Answer"}, "prompt_eval_count": 20, "eval_count": 5}, "/api/chat"),
            ("openai_compatible", {"choices": [{"finish_reason": "stop", "message": {"content": "Answer"}}],
                                   "usage": {"prompt_tokens": 20, "completion_tokens": 5}}, "/chat/completions"),
        ):
            connection = {"kind": kind, "base_url": "http://localhost:8002/prefix", "context_tokens": 4096, "output_tokens": 1000, "api_key": SECRET}
            upstream, requests, _ = self.upstream(reply)
            with upstream:
                result = await provider_transport.infer(connection, "custom/model:latest", "Follow instructions", {"problem": "Hello"})
            self.assertEqual(result, {"text": "Answer", "input_tokens": 20, "output_tokens": 5})
            self.assertEqual(requests[0].url.path, "/prefix" + path)
            body = json.loads(requests[0].content)
            self.assertEqual(body["messages"], [{"role": "system", "content": "Follow instructions"},
                                               {"role": "user", "content": '{"problem":"Hello"}'}])
            self.assertEqual(body["model"], "custom/model:latest")
            self.assertIs(body["stream"], False)
            if kind == "ollama":
                self.assertEqual(body["options"], {"num_ctx": 4096, "num_predict": 1000})
                self.assertNotIn("max_tokens", body)
            else:
                self.assertEqual(body["max_tokens"], 1000)
                self.assertNotIn("options", body)
            self.assertNotIn(SECRET, requests[0].content.decode())
            self.secret_free(result)

    async def test_chat_token_parameter_matches_model_family(self):
        connection = {"kind": "openai_compatible", "base_url": "http://localhost:8002/v1",
                      "context_tokens": 4096, "output_tokens": 1000}
        reply = {"choices": [{"finish_reason": "stop", "message": {"content": "Answer"}}]}
        for model, expected in (("gpt-5-mini", "max_completion_tokens"), ("openai/gpt-5.4", "max_completion_tokens"),
                                ("o3-mini", "max_completion_tokens"), ("gpt-4o-mini", "max_tokens"),
                                ("qwen2.5:7b-instruct", "max_tokens")):
            upstream, requests, _ = self.upstream(reply)
            with upstream:
                await provider_transport.infer(connection, model, "instructions", {})
            body = json.loads(requests[0].content)
            self.assertEqual(body[expected], 1000)
            self.assertEqual(set(body) & {"max_tokens", "max_completion_tokens"}, {expected})

    async def test_chat_rejects_legacy_completion_ids_before_network(self):
        for model in ("gpt-3.5-turbo-instruct", "openai/gpt-3.5-turbo-instruct-0914", "text-davinci-003"):
            with patch.object(provider_transport, "_request", new_callable=AsyncMock) as request:
                with self.assertRaises(providers.ProviderError) as raised:
                    await provider_transport.infer({"kind": "openai_compatible", "base_url": "http://localhost:8002/v1"},
                                                   model, "instructions", {})
                self.assertEqual(raised.exception.kind, "unsupported_model")
                request.assert_not_awaited()

    async def test_astra_chat_settings_use_profile_and_preserve_connection_budget(self):
        connection = {"kind": "openai_compatible", "base_url": "http://localhost:8002/v1", "context_tokens": 32768, "output_tokens": 8192}
        reply = {"choices": [{"finish_reason": "stop", "message": {"content": "Answer"}}]}
        upstream, requests, factory = self.upstream(reply)
        with upstream:
            await provider_transport.infer(connection, "openai/gpt-6-astra", "instructions", {})
        body = json.loads(requests[0].content)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["max_completion_tokens"], 8192)
        self.assertEqual(factory.call_args.kwargs["timeout"].read, 300)

    async def test_incomplete_chat_answer_keeps_usage_and_budget_diagnostic(self):
        reply = {"choices": [{"finish_reason": "length", "message": {"content": ""}}], "usage": {"prompt_tokens": 30, "completion_tokens": 2048}}
        upstream, _, _ = self.upstream(reply)
        with upstream, self.assertRaises(providers.ProviderError) as raised:
            await provider_transport.infer({"kind": "openai_compatible", "base_url": "http://localhost:8002/v1"},
                                           "gpt-6-astra", "instructions", {})
        self.assertEqual(raised.exception.kind, "output_limit")
        self.assertEqual(raised.exception.usage["output_tokens"], 2048)
        self.assertIn("including reasoning", raised.exception.message)

    async def test_long_complete_answers_are_preserved_for_both_custom_protocols(self):
        answer = "Saved code draft\n" * 2000
        for kind, reply in (("ollama", {"done": True, "message": {"content": answer}}),
                            ("openai_compatible", {"choices": [{"finish_reason": "stop", "message": {"content": answer}}]})):
            upstream, _, _ = self.upstream(reply)
            with upstream:
                result = await provider_transport.infer({"kind": kind, "base_url": "http://localhost:8002"}, "model", "instructions", {})
            self.assertEqual(result["text"], answer)

    async def test_incomplete_refused_tools_empty_and_oversized_answers_rejected(self):
        ollama_replies = [{"done": False, "message": {"content": "partial"}},
                          {"done": True, "done_reason": "length", "message": {"content": "partial"}}]
        for content in ("", " ", "x" * (provider_transport.MAX_TEXT_CHARS + 1), None, SECRET):
            ollama_replies.append({"done": True, "message": {"content": content}})
        ollama_replies.append({"done": True, "message": {"content": "answer", "tool_calls": [{}]}})
        chat_replies = [{"choices": []}, {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}]
        for extra in ({"refusal": "Refused"}, {"tool_calls": [{}]}, {"function_call": {"name": "tool"}}, {"content": None}):
            chat_replies.append({"choices": [{"finish_reason": "stop", "message": {"content": "answer", **extra}}]})
        for kind, replies in (("ollama", ollama_replies), ("openai_compatible", chat_replies)):
            for reply in replies:
                upstream, requests, _ = self.upstream(reply)
                with upstream, self.assertRaises(providers.ProviderError) as error:
                    await provider_transport.infer({"kind": kind, "base_url": "http://localhost:8002", "api_key": SECRET}, "model", "instructions", {})
                self.assertEqual(error.exception.status, 502)
                self.assertNotIn(SECRET, error.exception.message)
                self.assertEqual(len(requests), 1)

    async def test_input_budgets_reject_without_network_or_truncation(self):
        connection = {"kind": "ollama", "base_url": "http://localhost:11434", "api_key": SECRET}
        cases = [(connection, "model", "instructions", {"text": "x" * 200001}),
                 ({**connection, "context_tokens": 40, "output_tokens": 20}, "model", "instructions", {"text": "hello " * 30}),
                 (connection, "", "instructions", {}), (connection, SECRET, "instructions", {}),
                 (connection, "model", "instructions", {"key": SECRET}), (connection, "model", "instructions", float("nan"))]
        with patch.object(provider_transport.httpx, "AsyncClient", side_effect=AssertionError("Unexpected network")):
            for args in cases:
                with self.assertRaises(providers.ProviderError) as error:
                    await provider_transport.infer(*args)
                self.assertEqual(error.exception.status, 422)
                self.assertNotIn(SECRET, error.exception.message)

    async def test_transport_sanitizes_status_network_protocol_and_json_errors(self):
        connection = {"kind": "ollama", "base_url": "http://localhost:11434", "api_key": SECRET}
        for status, expected in ((401, 401), (403, 401), (429, 429), (500, 502), (302, 502)):
            upstream, requests, _ = self.upstream({"error": SECRET}, status=status)
            with upstream, self.assertRaises(providers.ProviderError) as error:
                await provider_transport.discover(connection)
            self.assertEqual(error.exception.status, expected)
            self.assertNotIn(SECRET, error.exception.message)
            self.assertEqual(len(requests), 1)
        for raised, expected in ((httpx.ConnectError(SECRET), 502), (httpx.ReadTimeout(SECRET), 504), (httpx.UnsupportedProtocol(SECRET), 502)):
            upstream, _, _ = self.upstream(error=raised)
            with upstream, self.assertRaises(providers.ProviderError) as error:
                await provider_transport.discover(connection)
            self.assertEqual(error.exception.status, expected)
            self.assertNotIn(SECRET, error.exception.message)
        for payload in (SECRET, {"models": SECRET}, {"models": [SECRET]}):
            upstream, _, _ = self.upstream(payload)
            with upstream, self.assertRaises(providers.ProviderError) as error:
                await provider_transport.discover(connection)
            self.assertEqual(error.exception.status, 502)
            self.assertNotIn(SECRET, error.exception.message)
        with self.assertRaises(providers.ProviderError) as error:
            await provider_transport.discover({**connection, "kind": "unsupported"})
        self.assertEqual(error.exception.status, 422)

    async def test_response_stream_limit_and_total_timeout(self):
        class LargeBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(100):
                    yield b"x" * 65536

        async def large(request):
            return httpx.Response(200, stream=LargeBody())

        connection = {"kind": "ollama", "base_url": "http://localhost:11434"}
        upstream, requests, _ = self.upstream(handler=large)
        with upstream, self.assertRaises(providers.ProviderError) as error:
            await provider_transport.discover(connection)
        self.assertEqual(error.exception.status, 502)
        self.assertIn("1 MiB", error.exception.message)
        self.assertEqual(len(requests), 1)

        async def slow(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json={"models": []})

        upstream, _, _ = self.upstream(handler=slow)
        with upstream, patch.object(provider_transport, "REQUEST_TIMEOUT", 0.001), self.assertRaises(providers.ProviderError) as error:
            await provider_transport.discover(connection)
        self.assertEqual(error.exception.status, 504)

    async def test_guards_cover_new_endpoints(self):
        for method, path in (("POST", "/profiles"), ("POST", "/profiles/test"), ("PUT", "/profiles/profile_bad"),
                             ("GET", "/routing"), ("PUT", "/routing")):
            response = await self.client.request(method, "/api/providers" + path, json=PROFILE, headers={"Origin": "https://evil.example"})
            self.assertEqual(response.status_code, 403)
            self.secret_free(response)

    async def test_multiple_saved_keys_are_isolated_and_task_urls_cannot_override_endpoint(self):
        first = await self.create(api_key=SECRET, storage="session")
        second = await self.create(name="Other instance", kind="openai_compatible", base_url="https://other.example/v1", api_key=OTHER)
        for identifier, key, endpoint in ((first, SECRET, "http://localhost:11434/api/chat"),
                                          (second, OTHER, "https://other.example/v1/chat/completions")):
            reply = {"done": True, "done_reason": "stop", "message": {"content": "Answer"},
                     "choices": [{"finish_reason": "stop", "message": {"content": "Answer"}}]}
            upstream, requests, factory = self.upstream(reply)
            with upstream, patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.invalid", "HTTPS_PROXY": "http://proxy.invalid",
                                                   "ALL_PROXY": "http://proxy.invalid", "OPENAI_API_KEY": "builtin-secret"}):
                await provider_transport.infer(self.settings.connection(identifier), "model", "instructions",
                                               {"base_url": "https://evil.example", "url": "http://169.254.169.254"})
            self.assertEqual(str(requests[0].url), endpoint)
            self.assertEqual(requests[0].headers["authorization"], "Bearer " + key)
            self.assertFalse(factory.call_args.kwargs["trust_env"])
            self.assertNotIn("builtin-secret", str(requests[0].headers))

    async def test_redirect_location_cannot_receive_credential(self):
        async def redirect(request):
            return httpx.Response(307, headers={"Location": "https://evil.example/" + SECRET})

        upstream, requests, _ = self.upstream(handler=redirect)
        with upstream, self.assertRaises(providers.ProviderError) as error:
            await provider_transport.discover({"kind": "ollama", "base_url": "http://localhost:11434", "api_key": SECRET})
        self.assertEqual(len(requests), 1)
        self.assertNotIn(SECRET, error.exception.message)

    async def test_catalog_metadata_never_resolves_credentials(self):
        local = await self.create()
        remote = await self.create(name="Hosted", kind="openai_compatible", base_url="https://remote.example/v1",
                                   api_key=SECRET, context_tokens=16384, output_tokens=4096, model="custom/model")
        self.settings.save("openai", OTHER, "session", "fixed-model", False)
        self.backend.reset_mock()
        fresh = providers.ProviderSettings(self.path)
        with patch.object(fresh, "_backend", side_effect=AssertionError("Keyring probe")), \
                patch.object(fresh, "_resolve", side_effect=AssertionError("Credential access")), \
                patch.object(fresh, "_profile_resolve", side_effect=AssertionError("Credential access")), \
                patch.object(providers.os.environ, "get", side_effect=AssertionError("Environment access")), \
                patch.object(fresh, "_sessions", None):
            rows = fresh.catalog()
        self.backend.get_password.assert_not_called()
        self.assertEqual([row["id"] for row in rows], ["openai", "anthropic", local, remote])
        for row in rows:
            self.assertEqual(set(row), {"id", "name", "kind", "base_url", "is_local", "default_model",
                                        "requires_key", "context_tokens", "output_tokens", "allowed_models"})
        self.assertEqual(rows[0], {"id": "openai", "name": "OpenAI", "kind": "openai",
                                  "base_url": "https://api.openai.com/v1", "is_local": False,
                                  "default_model": "fixed-model", "requires_key": True,
                                  "context_tokens": 8192, "output_tokens": 2048, "allowed_models": []})
        self.assertTrue(rows[2]["is_local"])
        self.assertFalse(rows[2]["requires_key"])
        self.assertEqual(rows[3]["context_tokens"], 16384)
        self.assertEqual(rows[3]["output_tokens"], 4096)
        self.assertTrue(rows[3]["requires_key"])
        self.secret_free(rows)

    async def test_cloud_discovery_metadata_and_local_only_filter(self):
        items = [{"name": "llama:latest"}, {"name": "gpt-oss:120b-cloud"}, {"name": "qwen:cloud"},
                 {"name": "alias", "remote_host": "https://cloud.example/" + SECRET},
                 {"name": "nested", "data": {"remote_model": "qwen:cloud"}},
                 {"name": "reported", "remote_model": "remote-model"}]
        connection = {"kind": "ollama", "base_url": "http://localhost:11434", "api_key": SECRET}
        for local_only in (False, True):
            upstream, requests, _ = self.upstream({"models": items})
            with upstream:
                result = await provider_transport.discover({**connection, "local_only": local_only})
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].url.path, "/api/tags")
            models = result["models"]
            self.assertEqual(models[0], {"id": "llama:latest", "name": "llama:latest", "is_local": True, "remote": False})
            self.assertEqual(len(models), 1 if local_only else len(items))
            for item in models[1:]:
                self.assertFalse(item["is_local"])
                self.assertTrue(item["remote"])
                self.assertFalse(provider_transport.model_is_local(connection, item))
            self.assertIn("proxy", result["message"])
            self.assertIn("excluded" if local_only else "Cloud-backed models may send prompts", result["message"])
            self.secret_free(result)

    async def test_model_locality_uses_endpoint_and_reports_not_spoofed_connection_flag(self):
        local = {"kind": "ollama", "base_url": "http://localhost:11434"}
        for item in ({"id": "ordinary", "remote_host": "remote.example"}, {"id": "GPT-OSS:120B-CLOUD"},
                     {"id": "qwen:CLOUD"}, {"id": "alias", "details": {"remote_model": "other"}},
                     {"id": "ordinary", "is_local": False}, {"id": "ordinary", "remote": True}):
            self.assertFalse(provider_transport.model_is_local(local, item))
        self.assertTrue(provider_transport.model_is_local(local, {"id": "local", "remote_host": "", "remote_model": None}))
        self.assertFalse(provider_transport.model_is_local({**local, "base_url": "https://remote.example", "is_local": True}, {"id": "local"}))

    async def test_discovery_model_limit_and_remote_compatible_locality(self):
        upstream, _, _ = self.upstream({"data": [{"id": f"model-{i}"} for i in range(501)]})
        with upstream:
            result = await provider_transport.discover({"kind": "openai_compatible", "base_url": "https://remote.example/v1"})
        self.assertEqual(len(result["models"]), 500)
        self.assertTrue(all(item["is_local"] is False for item in result["models"]))
        self.assertIn("truncated", result["message"])

    async def test_local_only_known_cloud_and_remote_endpoints_never_send(self):
        connection = {"kind": "ollama", "base_url": "http://localhost:11434", "local_only": True}
        for model, conn in (("gpt-oss:120b-cloud", connection), ("qwen:cloud", connection),
                            ("model", {**connection, "base_url": "https://remote.example"}),
                            ("model", {**connection, "kind": "openai_compatible", "base_url": "https://remote.example/v1"})):
            with patch.object(provider_transport.httpx, "AsyncClient", side_effect=AssertionError("Outbound request")), \
                    self.assertRaises(providers.ProviderError) as error:
                await provider_transport.infer(conn, model, "instructions", {})
            self.assertEqual(error.exception.status, 422)

    async def test_local_only_show_blocks_remote_alias_and_bad_show_without_chat(self):
        connection = {"kind": "ollama", "base_url": "http://localhost:11434", "local_only": True}
        replies = [{"remote_host": "https://cloud.example"}, {"data": {"remote_host": "https://cloud.example"}},
                   {"remote_model": "remote-model"}, {"model_info": {"remote_model": "remote"}},
                   {"model": "gpt-oss:120b-cloud"}, {}, {"error": SECRET}]
        for reply in replies:
            upstream, requests, _ = self.upstream(reply)
            authorization = AsyncMock(return_value=connection)
            with upstream, self.assertRaises(providers.ProviderError) as error:
                await provider_transport.infer(connection, "innocent-alias", "instructions", {}, before_send=authorization)
            self.assertIn(error.exception.status, (422, 502))
            self.assertNotIn(SECRET, error.exception.message)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].url.path, "/api/show")
            self.assertEqual(json.loads(requests[0].content), {"model": "innocent-alias"})
            self.assertEqual(authorization.await_count, 2)

    async def test_local_only_show_then_chat_reauthorizes_and_refreshes_key(self):
        connection = {"id": "profile_test", "kind": "ollama", "base_url": "http://localhost:11434", "local_only": True,
                      "api_key": SECRET, "context_tokens": 8192, "output_tokens": 2048}
        events = []

        async def respond(request):
            events.append(request.url.path)
            if request.url.path == "/api/show":
                self.assertEqual(request.headers["authorization"], "Bearer " + SECRET)
                return httpx.Response(200, json={"details": {"family": "llama"}, "model_info": {}})
            self.assertEqual(request.headers["authorization"], "Bearer " + OTHER)
            return httpx.Response(200, json={"done": True, "message": {"content": "answer"}})

        async def authorize():
            events.append("authorize")
            # Preserve the local-only guard even if a consumer accidentally omits the flag.
            return {key: (OTHER if key == "api_key" and "/api/show" in events else value)
                    for key, value in connection.items() if key != "local_only"}

        upstream, requests, _ = self.upstream(handler=respond)
        with upstream:
            result = await provider_transport.infer(connection, "llama:latest", "instructions", {}, before_send=authorize)
        self.assertEqual(result, {"text": "answer"})
        self.assertEqual(events, ["authorize", "/api/show", "authorize", "/api/chat"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(connection["api_key"], SECRET)

    async def test_authorization_runs_after_token_preparation_and_prevents_send(self):
        connection = {"kind": "openai_compatible", "base_url": "http://localhost:8002/v1"}
        events = []
        original = provider_transport._messages

        def prepare(*args):
            result = original(*args)
            events.append("prepared")
            return result

        async def authorize():
            events.append("authorize")
            raise providers.ProviderError(409, "Routing authorization was revoked.")

        with patch.object(provider_transport, "_messages", side_effect=prepare), \
                patch.object(provider_transport.httpx, "AsyncClient", side_effect=AssertionError("Outbound request")), \
                self.assertRaises(providers.ProviderError):
            await provider_transport.infer(connection, "model", "instructions", {}, before_send=authorize)
        self.assertEqual(events, ["prepared", "authorize"])

    async def test_fresh_authorization_rejects_changed_runtime_and_new_key_in_prepared_input(self):
        connection = {"id": "profile_test", "kind": "openai_compatible", "base_url": "http://localhost:8002/v1", "api_key": SECRET,
                      "context_tokens": 8192, "output_tokens": 2048}
        changes = [{"id": "different"}, {"base_url": "http://localhost:8003/v1"}, {"kind": "ollama"},
                   {"context_tokens": 4096}, {"output_tokens": 1024}, {"is_local": False}, {"requires_key": True}, {"api_key": OTHER}]
        for fields in changes:
            with patch.object(provider_transport.httpx, "AsyncClient", side_effect=AssertionError("Outbound request")), \
                    self.assertRaises(providers.ProviderError) as error:
                await provider_transport.infer(connection, "model", "instructions", {"value": OTHER},
                                               before_send=AsyncMock(return_value={**connection, **fields}))
            self.assertEqual(error.exception.status, 422 if "api_key" in fields else 409)
            self.assertNotIn(SECRET, error.exception.message)
            self.assertNotIn(OTHER, error.exception.message)

    async def test_authorization_revocation_or_cancellation_after_show_never_chats(self):
        connection = {"kind": "ollama", "base_url": "http://localhost:11434", "local_only": True}
        for revoked in (providers.ProviderError(409, "Routing was revoked."), asyncio.CancelledError()):
            upstream, requests, _ = self.upstream({"details": {}})
            authorize = AsyncMock(side_effect=[connection, revoked])
            with upstream, self.assertRaises(type(revoked)):
                await provider_transport.infer(connection, "local", "instructions", {}, before_send=authorize)
            self.assertEqual([r.url.path for r in requests], ["/api/show"])

    async def test_explicit_cloud_selection_is_not_forced_into_local_only(self):
        connection = {"kind": "ollama", "base_url": "http://localhost:11434"}
        upstream, requests, _ = self.upstream({"done": True, "message": {"content": "answer"}})
        with upstream:
            result = await provider_transport.infer(connection, "gpt-oss:120b-cloud", "instructions", {})
        self.assertEqual(result, {"text": "answer"})
        self.assertEqual([r.url.path for r in requests], ["/api/chat"])

    async def test_allowed_models_persist_and_omitted_updates_preserve_restriction(self):
        allowed = ["free/model", "llama:latest"]
        identifier = await self.create(allowed_models=allowed)
        self.assertEqual(self.settings.connection(identifier)["allowed_models"], allowed)
        self.assertEqual(self.settings.status()["providers"][-1]["allowed_models"], allowed)
        self.assertEqual(self.settings.catalog()[-1]["allowed_models"], allowed)
        self.assertEqual(json.loads(self.path.read_text())["profiles"][identifier]["allowed_models"], allowed)
        self.assertEqual(providers.ProviderSettings(self.path).connection(identifier)["allowed_models"], allowed)
        response = await self.update(identifier, name="Renamed")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.settings.connection(identifier)["allowed_models"], allowed)
        self.settings.save("openai", SECRET, "session", "fixed-model", False)
        self.settings.dismiss(True)
        self.assertEqual(self.settings.catalog()[-1]["allowed_models"], allowed)
        response = await self.update(identifier, allowed_models=[])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.settings.connection(identifier)["allowed_models"], [])

    async def test_legacy_profiles_default_allowed_models_to_empty(self):
        identifier = await self.create()
        raw = json.loads(self.path.read_text())
        del raw["profiles"][identifier]["allowed_models"]
        self.path.write_text(json.dumps(raw))
        fresh = providers.ProviderSettings(self.path)
        self.assertEqual(fresh.connection(identifier)["allowed_models"], [])
        self.assertTrue(all(row["allowed_models"] == [] for row in fresh.catalog()))
        self.assertTrue(all(row["allowed_models"] == [] for row in fresh.status()["providers"]))
        fresh.dismiss(True)
        self.assertEqual(json.loads(self.path.read_text())["profiles"][identifier]["allowed_models"], [])
        raw["profiles"][identifier]["allowed_models"] = ["duplicate", "duplicate"]
        self.path.write_text(json.dumps(raw))
        with self.assertRaises(providers.ProviderError) as error:
            fresh.catalog()
        self.assertEqual(error.exception.status, 503)

    async def test_allowed_models_validation_rejects_duplicates_bad_ids_and_secrets(self):
        invalid = [["model", "model"], ["bad model"], [""], ["x" * 201], [None], [1], [{}],
                   "model", None, [f"model-{i}" for i in range(51)], [SECRET], ["prefix/" + SECRET]]
        for value in invalid:
            response = await self.client.post("/api/providers/profiles", json={**PROFILE, "api_key": SECRET, "allowed_models": value})
            self.assertEqual(response.status_code, 422, response.text)
            self.secret_free(response)
            self.assertFalse(self.path.exists())
        identifier = await self.create(api_key=SECRET, allowed_models=["free-model"])
        previous = self.path.read_bytes()
        for value in ([SECRET], ["free-model", "free-model"]):
            response = await self.update(identifier, allowed_models=value)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.path.read_bytes(), previous)
            self.secret_free(response)
        # A new credential cannot turn previously non-secret metadata into a stored secret.
        identifier = await self.create(allowed_models=[OTHER])
        previous = self.path.read_bytes()
        response = await self.update(identifier, api_key=OTHER)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn(OTHER, response.text)
        self.assertEqual(self.path.read_bytes(), previous)

    async def test_allowed_models_discovery_filters_saved_and_unsaved_profiles(self):
        for kind, payload in (("ollama", {"models": [{"name": "free-model"}, {"name": "paid-model"}]}),
                              ("openai_compatible", {"data": [{"id": "free-model"}, {"id": "paid-model"}]})):
            identifier = await self.create(kind=kind, allowed_models=["free-model", "not-installed"])
            for path, body in ((f"/{identifier}/test", {}),
                               ("/profiles/test", {**PROFILE, "kind": kind, "allowed_models": ["free-model", "not-installed"]})):
                upstream, requests, _ = self.upstream(payload)
                with upstream:
                    response = await self.client.post("/api/providers" + path, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["models"], [{"id": "free-model", "name": "free-model", "is_local": True, "remote": False}])
                self.assertIn("allowed models", response.json()["message"])
                self.assertNotIn("paid-model", response.text)
                self.assertEqual(len(requests), 1)
            upstream, _, _ = self.upstream(payload)
            with upstream:
                result = await provider_transport.discover({**self.settings.connection(identifier), "allowed_models": []})
            self.assertEqual([item["id"] for item in result["models"]], ["free-model", "paid-model"])

    async def test_disallowed_model_inference_never_sends_for_either_protocol(self):
        for kind in ("ollama", "openai_compatible"):
            connection = {"kind": kind, "base_url": "http://localhost:11434", "allowed_models": ["free-model"], "local_only": True}
            with patch.object(provider_transport.httpx, "AsyncClient", side_effect=AssertionError("Outbound request")), \
                    self.assertRaises(providers.ProviderError) as error:
                await provider_transport.infer(connection, "paid-model", "instructions", {})
            self.assertEqual(error.exception.status, 422)
            self.assertNotIn("paid-model", error.exception.message)

    async def test_allowlist_rechecked_after_authorization_and_show(self):
        for kind in ("ollama", "openai_compatible"):
            connection = {"kind": kind, "base_url": "http://localhost:11434", "allowed_models": [], "local_only": True}
            fresh = {**connection, "allowed_models": ["other-model"]}
            with patch.object(provider_transport.httpx, "AsyncClient", side_effect=AssertionError("Outbound request")), \
                    self.assertRaises(providers.ProviderError) as error:
                await provider_transport.infer(connection, "free-model", "instructions", {}, before_send=AsyncMock(return_value=fresh))
            self.assertEqual(error.exception.status, 422)
        connection = {"kind": "ollama", "base_url": "http://localhost:11434", "allowed_models": ["free-model"], "local_only": True}
        upstream, requests, _ = self.upstream({"details": {}})
        authorize = AsyncMock(side_effect=[connection, {**connection, "allowed_models": ["other-model"]}])
        with upstream, self.assertRaises(providers.ProviderError) as error:
            await provider_transport.infer(connection, "free-model", "instructions", {}, before_send=authorize)
        self.assertEqual(error.exception.status, 422)
        self.assertEqual([request.url.path for request in requests], ["/api/show"])

    async def test_allowed_models_are_part_of_connection_fingerprint(self):
        connection = {"kind": "openai_compatible", "base_url": "http://localhost:11434", "allowed_models": ["free-model"]}
        # Even broadening the list requires current settings to be selected again.
        authorize = AsyncMock(return_value={**connection, "allowed_models": []})
        with patch.object(provider_transport.httpx, "AsyncClient", side_effect=AssertionError("Outbound request")), \
                self.assertRaises(providers.ProviderError) as error:
            await provider_transport.infer(connection, "free-model", "instructions", {}, before_send=authorize)
        self.assertEqual(error.exception.status, 409)


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    setUp = ProfileTests.setUp
    upstream = ProfileTests.upstream

    async def diagnostic(self, payload=None, status=400, *, handler=None, kind="ollama"):
        connection = {"kind": kind, "base_url": "http://localhost:11434/private-url-token", "api_key": SECRET}
        suffix = "/api/chat" if kind == "ollama" else "/chat/completions"
        upstream, requests, factory = self.upstream(payload, status=status, handler=handler)
        with upstream, self.assertRaises(providers.ProviderError) as raised:
            await provider_transport._request(connection, "POST", suffix, {"input": "private-user-input"})
        error = raised.exception
        self.assertIn(f"HTTP {status}", error.message)
        self.assertIn("POST " + suffix, error.message)
        self.assertEqual(len(requests), 1)
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])
        self.assertLessEqual(len(error.message), 400)
        for secret in (SECRET, OTHER, "private-url-token", "private-user-input", "localhost", "unrecognized-code"):
            self.assertNotIn(secret, error.message)
        return error

    async def test_diagnostic_allowlisted_codes_and_types(self):
        cases = [(404, "model_not_found", "Model not found"), (400, "model_unavailable", "unavailable"),
                 (400, "unsupported_model", "does not support chat"),
                 (400, "context_length_exceeded", "Context length exceeded"),
                 (429, "insufficient_quota", "credits"), (400, "out_of_memory", "OOM"),
                 (429, "rate_limit_exceeded", "Rate limit"), (503, "overloaded_error", "overloaded")]
        for kind in ("ollama", "openai_compatible"):
            for status, code, category in cases:
                for field in ("code", "type"):
                    with self.subTest(kind=kind, status=status, field=field, code=code):
                        error = await self.diagnostic({"error": {field: code, "message": SECRET + OTHER}}, status, kind=kind)
                        self.assertIn(category, error.message)
                        self.assertEqual(error.status, 429 if status == 429 else 502)

    async def test_diagnostic_native_known_messages_are_classified_without_echo(self):
        cases = [(f"model '{SECRET}' not found, try pulling it first", "Model not found"),
                 (f'"{SECRET}" does not support chat', "does not support chat"),
                 ("model only supports embeddings", "does not support chat"),
                 ("This is not a chat model and thus not supported in the v1/chat/completions endpoint. Did you mean to use v1/completions?", "does not support chat"),
                 ("context length exceeded", "Context length exceeded"),
                 ("insufficient credits", "credits"), ("CUDA out of memory", "OOM"),
                 ("model requires more system memory (12.0 GiB) than is available (8.0 GiB)", "OOM"),
                 ("failed to allocate GPU memory", "OOM"), ("rate limit exceeded", "Rate limit"),
                 ("server is overloaded", "overloaded")]
        for message, category in cases:
            for value in (message, {"message": message, "code": "unrecognized-code"}):
                with self.subTest(message=message, nested=isinstance(value, dict)):
                    error = await self.diagnostic({"error": value})
                    self.assertIn(category, error.message)

    async def test_diagnostic_unknown_nested_and_malformed_bodies_are_never_echoed(self):
        values = [{"error": {"code": {"secret": SECRET}, "type": [OTHER], "message": {"text": SECRET}}},
                  {"error": {"code": SECRET, "type": "unrecognized-code", "message": OTHER}},
                  {"error": {"code": "model_not_found/" + SECRET, "type": True, "message": [OTHER]}},
                  {"error": {"message": "User input says out of memory " + SECRET}},
                  {"error": {"message": "model not found " + SECRET}},
                  {"error": {"message": "model not found" + " " * 2049}},
                  {"error": [SECRET, {"message": OTHER}]}]
        for value in values:
            error = await self.diagnostic(value)
            self.assertIn("Provider request failed", error.message)
        for content in (b"<html>" + SECRET.encode() + b"</html>", b'{"error":', b"\xff", b"[]",
                        b"[" * 2000 + b"]" * 2000):
            async def malformed(request):
                return httpx.Response(404, content=content)
            error = await self.diagnostic(status=404, handler=malformed)
            self.assertIn("Error body", error.message)

    async def test_diagnostic_http_defaults_and_redirect_headers(self):
        for status, app_status, category in ((401, 401, "Authentication failed"), (403, 401, "Access denied"),
                                           (402, 502, "Payment required"), (429, 429, "Rate limit"),
                                           (503, 502, "overloaded"), (500, 502, "Provider request failed"),
                                           (301, 502, "redirects are not followed"), (307, 502, "redirects are not followed")):
            async def response(request):
                return httpx.Response(status, json={"error": SECRET}, headers={
                    "Location": "https://evil.invalid/" + OTHER,
                    "Retry-After": SECRET + "\t999999999999999999999"})
            error = await self.diagnostic(status=status, handler=response)
            self.assertEqual(error.status, app_status)
            self.assertIn(category, error.message)

    async def test_diagnostic_stream_limit_applies_to_success_and_error_before_parsing(self):
        for status in (200, 400):
            chunks = []
            class LargeBody(httpx.AsyncByteStream):
                async def __aiter__(self):
                    for index in range(100):
                        chunks.append(index)
                        yield b"x" * 65536
            async def response(request):
                return httpx.Response(status, stream=LargeBody())
            error = await self.diagnostic(status=status, handler=response)
            self.assertIn("1 MiB", error.message)
            self.assertEqual(len(chunks), 17)
            self.assertNotIn("invalid JSON", error.message)
        # A valid object exactly at the limit is accepted for parsing on both paths.
        for status in (200, 400):
            content = b'{"padding":"' + b"x" * (provider_transport.MAX_RESPONSE_BYTES - 14) + b'"}'
            self.assertEqual(len(content), provider_transport.MAX_RESPONSE_BYTES)
            async def boundary(request):
                return httpx.Response(status, content=content)
            if status == 400:
                error = await self.diagnostic(status=status, handler=boundary)
                self.assertNotIn("1 MiB", error.message)
            else:
                upstream, _, _ = self.upstream(handler=boundary)
                with upstream:
                    result = await provider_transport._request({"kind": "ollama", "base_url": "http://localhost:11434"}, "GET", "/api/tags")
                self.assertEqual(len(result["padding"]), provider_transport.MAX_RESPONSE_BYTES - 14)

    async def test_diagnostic_error_read_timeout_and_network_error_preserve_known_status(self):
        for failure in (httpx.ReadTimeout(SECRET), httpx.ReadError(SECRET), "deadline"):
            class BrokenBody(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'{"error":'
                    if failure == "deadline":
                        await asyncio.Event().wait()
                    else:
                        raise failure
            async def response(request):
                return httpx.Response(503, stream=BrokenBody())
            with patch.object(provider_transport, "REQUEST_TIMEOUT", .05):
                error = await self.diagnostic(status=503, handler=response)
            self.assertEqual(error.status, 502)
            self.assertIn("overloaded", error.message)
        for failure, expected in ((httpx.ReadTimeout(SECRET), 504), (httpx.ConnectError(OTHER), 502)):
            upstream, requests, _ = self.upstream(error=failure)
            with upstream, self.assertRaises(providers.ProviderError) as raised:
                await provider_transport._request({"kind": "ollama", "base_url": "http://localhost:11434"}, "POST", "/api/chat")
            self.assertEqual(raised.exception.status, expected)
            self.assertNotIn("HTTP", raised.exception.message)
            self.assertNotIn(SECRET, raised.exception.message)
            self.assertNotIn(OTHER, raised.exception.message)
            self.assertEqual(len(requests), 1)

    async def test_diagnostic_error_usage_accepts_only_nonnegative_integers(self):
        for payload, expected in (({"prompt_eval_count": 7, "eval_count": 2}, {"input_tokens": 7, "output_tokens": 2}),
                                  ({"usage": {"prompt_tokens": 9, "completion_tokens": 0, "secret": SECRET}}, {"input_tokens": 9, "output_tokens": 0}),
                                  ({"error": {"usage": {"input_tokens": 3, "output_tokens": 1}}}, {"input_tokens": 3, "output_tokens": 1})):
            error = await self.diagnostic(payload)
            self.assertEqual(error.usage, expected)
        for invalid in (True, -1, 1.5, "12", None, [SECRET], {"value": OTHER}):
            error = await self.diagnostic({"usage": {"prompt_tokens": invalid, "completion_tokens": invalid},
                                           "prompt_eval_count": invalid, "eval_count": invalid})
            self.assertEqual(error.usage, {})

    async def test_diagnostic_http_200_error_envelope_and_invalid_json_are_distinct(self):
        for kind in ("ollama", "openai_compatible"):
            error = await self.diagnostic({"error": {"code": "context_length_exceeded", "message": SECRET},
                                           "usage": {"prompt_tokens": 8, "completion_tokens": 0}}, 200, kind=kind)
            self.assertIn("Context length exceeded", error.message)
            self.assertEqual(error.usage, {"input_tokens": 8, "output_tokens": 0})
            async def invalid(request):
                return httpx.Response(200, content=SECRET.encode())
            error = await self.diagnostic(status=200, handler=invalid, kind=kind)
            self.assertIn("invalid JSON", error.message)


if __name__ == "__main__":
    unittest.main()
