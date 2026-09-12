import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import api
import provider_api
import providers
import storage
import task_api
import task_models as models
import task_runner
from task_store import TaskStore, EVENT_LIMIT, event


SECRET = "test-credential-not-a-real-key"
STORE_ID = str(uuid4())
OTHER_STORE = str(uuid4())


def planned(task_id="a", deps=None):
    return {"id": task_id, "title": "Draft " + task_id, "instructions": "Write a useful draft.",
            "depends_on": deps or [], "acceptance_criteria": ["Explain assumptions."]}


def review(approved=True, revisions=None):
    return {"approved": approved, "feedback": "Accepted." if approved else "Clarify assumptions.",
            "revision_tasks": revisions or [], "final_output": "Synthesized draft." if approved else None}


def discovered(*ids):
    return {"models": [{"id": value, "name": value} for value in ids]}


class ModelTests(unittest.TestCase):
    def test_selection_filters_and_smaller_workers(self):
        result = models.model_options("openai", discovered("gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4o",
            "gpt-5-codex", "gpt-4o-audio-preview", "gpt-image-1", "text-embedding-3-large", "o3-deep-research",
            "whisper-1", "gpt-random", "o3", "gpt-3.5-turbo"))
        self.assertEqual({m["id"] for m in result["models"]}, {"gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4o", "o3"})
        self.assertEqual((result["lead_model"], result["worker_model"]), ("gpt-5", "gpt-5-mini"))
        self.assertIn("HEURISTIC", result["reason"])
        result = models.model_options("anthropic", discovered("claude-opus-4-1", "claude-sonnet-4-5", "claude-haiku-4-5", "claude-3-5-haiku-20241022", "claude-fake", "claude-sonnet-4-computer"))
        self.assertEqual((result["lead_model"], result["worker_model"]), ("claude-opus-4-1", "claude-haiku-4-5"))
        self.assertEqual(len(result["models"]), 4)
        result = models.model_options("anthropic", discovered("claude-opus-4-1", "claude-sonnet-4-5"))
        self.assertEqual(result["worker_model"], "claude-sonnet-4-5")
        self.assertEqual(models.model_options("openai", discovered("gpt-4.5-preview"))["lead_model"], "gpt-4.5-preview")

    def test_empty_discovery_no_invented_models_and_legacy_overrides(self):
        for values in ((), ("gpt-3.5-turbo",)):
            result = models.model_options("openai", {**discovered(*values), "message": "Truncated"})
            self.assertIsNone(result["lead_model"])
            self.assertIsNone(result["worker_model"])
            self.assertIn("truncated", result["reason"])
        with self.assertRaises(providers.ProviderError):
            models.model_options("copilot", discovered())

    def test_responses_incompatible_models_are_not_selectable(self):
        incompatible = ("o1-mini", "o1-mini-2024-09-12", "o1-preview", "o1-preview-2024-09-12",
                        "gpt-3.5-turbo", "gpt-3.5-turbo-0125", "gpt-4", "gpt-4-0613", "gpt-4-32k")
        supported = ("o1", "o1-2024-12-17", "o1-pro", "o3-mini", "gpt-4o", "gpt-4.1-mini", "gpt-5")
        result = models.model_options("openai", discovered(*incompatible, *supported))
        self.assertEqual({m["id"] for m in result["models"]}, set(supported))
        self.assertIn("endpoint-incompatible", result["reason"])

    def test_strict_inputs(self):
        for override in ({"max_tasks": 9}, {"max_tasks": 0}, {"max_tasks": True}, {"max_tasks": "6"},
                         {"max_revisions": 4}, {"max_revisions": -1}, {"provider": "copilot"},
                         {"api_key": SECRET}, {"goal": " "}, {"goal": "a" * 12001}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                models.RunInput.model_validate({"provider": "openai", "goal": "Draft", **override})
        body = models.RunInput(goal="Draft", provider="openai", lead_model=None, worker_model=None)
        self.assertEqual((body.max_tasks, body.max_revisions), (6, 2))

    def test_plan_validation(self):
        self.assertEqual(len(models.validate_plan(json.dumps({"tasks": [planned("b", ["a"]), planned()]}), 2).tasks), 2)
        for tasks in ([], [planned(), planned()], [planned("a", ["a"])], [planned("a", ["missing"])],
                      [planned("a", ["b"]), planned("b", ["a"])], [planned("b", ["a", "a"]), planned()],
                      [{**planned(), "extra": SECRET}], [{**planned(), "acceptance_criteria": []}],
                      [{**planned(), "id": "bad id"}]):
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                models.validate_plan(json.dumps({"tasks": tasks}), 8)
        with self.assertRaises(ValueError):
            models.validate_plan(json.dumps({"tasks": [planned(), planned("b")]}), 1)
        self.assertEqual(len(models.validate_plan('```json\n' + json.dumps({"tasks": [planned()]}) + '\n```', 1).tasks), 1)

    def test_review_validation(self):
        for valid in (review(), review(False, ["a"])):
            models.validate_review(json.dumps(valid), {"a"})
        for invalid in (review(True, ["a"]), review(False), review(False, ["missing"]), review(False, ["a", "a"]),
                        {**review(), "approved": "true"}, {**review(), "final_output": None},
                        {**review(False, ["a"]), "final_output": "not null"}, {**review(), "extra": SECRET}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                models.validate_review(json.dumps(invalid), {"a"})

    def test_input_budgets_without_tokenizer_network(self):
        with patch.object(models.tiktoken, "get_encoding") as encoding:
            encoding.return_value.encode.return_value = [0]
            self.assertEqual(json.loads(models.request_input("draft", {"goal": "x"})), {"goal": "x"})
            with self.assertRaises(models.InferenceError):
                models.request_input("draft", {"goal": "x" * models.MAX_INPUT_BYTES})
            encoding.return_value.encode.return_value = [0] * (models.MAX_INPUT_TOKENS + 1)
            with self.assertRaises(models.InferenceError):
                models.request_input("draft", {"goal": "x"})


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = TaskStore(Path(temp.name) / "tasks.json")
        self.body = models.RunInput(goal="Draft", provider="openai").model_dump()

    def test_immutable_terminal_atomic_claims_and_recent_history(self):
        first = self.store.create(STORE_ID, self.body)
        def claim(_):
            return self.store.update(STORE_ID, first["id"], lambda run: run.update(status="planning"), {"queued"})
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(value is not None for value in pool.map(claim, range(8))), 1)
        self.store.finish(STORE_ID, first["id"], "cancelled", "Cancelled.")
        before = self.store.get(STORE_ID, first["id"])
        self.assertIsNone(self.store.update(STORE_ID, first["id"], lambda run: run.update(status="completed")))
        self.store.interrupt_active()
        self.assertEqual(before, self.store.get(STORE_ID, first["id"]))
        for _ in range(55):
            run = self.store.create(STORE_ID, self.body)
            self.store.finish(STORE_ID, run["id"], "completed", "Finished.")
        self.assertEqual(len(self.store.list(STORE_ID)), 50)
        self.assertEqual(len(json.loads(self.store.path.read_text())["runs"]), 56)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_admission_and_events(self):
        for _ in range(20):
            run = self.store.create(STORE_ID, self.body)
        with self.assertRaises(providers.ProviderError) as error:
            self.store.create(STORE_ID, self.body)
        self.assertEqual(error.exception.status, 429)
        for _ in range(EVENT_LIMIT + 5):
            event(run, "Bounded")
        self.assertEqual(len(run["events"]), EVENT_LIMIT)

    def test_lease_prevents_second_owner_before_interrupting(self):
        other = TaskStore(self.store.path)
        self.store.acquire_owner()
        try:
            with self.assertRaises(providers.ProviderError):
                other.acquire_owner()
        finally:
            self.store.release_owner()
        other.acquire_owner()
        other.release_owner()

    def test_invalid_persistence_and_failed_atomic_replace(self):
        run = self.store.create(STORE_ID, self.body)
        original = self.store.path.read_bytes()
        with patch("task_store.os.replace", side_effect=OSError(SECRET)), self.assertRaises(providers.ProviderError) as error:
            self.store.finish(STORE_ID, run["id"], "failed", "Failed.")
        self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(self.store.path.read_bytes(), original)
        with patch.object(Path, "read_text", return_value="invalid"), self.assertRaises(providers.ProviderError):
            self.store.list(STORE_ID)

    def test_late_accounting_is_idempotent_and_cannot_mutate_terminal_state(self):
        for status in ("completed", "failed", "cancelled", "interrupted"):
            with self.subTest(status=status):
                run = self.store.create(STORE_ID, self.body)
                self.store.update(STORE_ID, run["id"], lambda r: r["usage"].update(model_calls=2, unknown_calls=2))
                self.store.finish(STORE_ID, run["id"], status, "Terminal.")
                before = self.store.get(STORE_ID, run["id"])
                self.store.account_usage(STORE_ID, run["id"], 1, {})
                self.store.account_usage(STORE_ID, run["id"], 1, {"input_tokens": 7, "text": SECRET})
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda _: TaskStore(self.store.path).account_usage(
                        STORE_ID, run["id"], 1, {"input_tokens": 7, "output_tokens": 4}), range(8)))
                self.store.account_usage(STORE_ID, run["id"], 1, {"input_tokens": 900, "output_tokens": 900})
                self.store.account_usage(STORE_ID, run["id"], 2, {"input_tokens": True, "output_tokens": -1, "text": SECRET})
                after = self.store.get(STORE_ID, run["id"])
                self.assertEqual(after["usage"], {"input_tokens": 7, "output_tokens": 4, "model_calls": 2, "unknown_calls": 1})
                self.assertEqual({k: v for k, v in before.items() if k != "usage"}, {k: v for k, v in after.items() if k != "usage"})
                self.assertNotIn("call_usage", after)
                self.assertNotIn(SECRET, self.store.path.read_text())
                with self.assertRaises(providers.ProviderError):
                    self.store.account_usage(OTHER_STORE, run["id"], 1, {})
                with self.assertRaises(providers.ProviderError):
                    self.store.account_usage(STORE_ID, run["id"], 3, {})


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.settings = providers.ProviderSettings(self.path / "providers.json")
        self.settings._sessions["openai"] = SECRET
        self.settings._sessions["anthropic"] = SECRET
        self.runner = task_runner.TaskRunner(TaskStore(self.path / "tasks.json"))
        self.plan = {"tasks": [planned()]}
        self.reviews = [review()]
        self.requests = []
        self.discovery = AsyncMock(return_value=discovered("gpt-5", "gpt-5-mini"))
        self.gateway = AsyncMock(side_effect=self.infer)
        self.existing = {STORE_ID, OTHER_STORE}
        for patcher in (patch.dict(os.environ, {}, clear=True), patch.object(providers, "keyring", None),
                        patch.object(provider_api, "settings", self.settings),
                        patch.object(storage, "get_store", side_effect=lambda sid: {"id": sid} if sid in self.existing else None),
                        patch.object(providers, "list_models", self.discovery), patch.object(models, "infer", self.gateway),
                        patch.object(task_runner, "runner", self.runner)):
            patcher.start()
            self.addCleanup(patcher.stop)
        await self.runner.startup()
        self.addAsyncCleanup(self.runner.shutdown)
        self.app = FastAPI(lifespan=task_runner.lifespan)
        self.app.include_router(provider_api.router)
        self.app.include_router(task_api.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 1234)), base_url="http://localhost:8000")
        self.addAsyncCleanup(self.client.aclose)
        self.url = f"/api/stores/{STORE_ID}/task-runs"

    async def infer(self, provider, credential, model, instructions, payload):
        self.assertEqual(credential, SECRET)
        self.requests.append((model, instructions, deepcopy(payload)))
        if instructions == task_runner.PLANNER:
            text = json.dumps(self.plan)
        elif instructions == task_runner.REVIEWER:
            text = json.dumps(self.reviews.pop(0))
        else:
            text = "Output " + payload["task"]["id"]
        return {"text": text, "input_tokens": 10, "output_tokens": 5}

    async def submit(self, **overrides):
        response = await self.client.post(self.url, json={"goal": "Draft a solution", "provider": "openai", **overrides})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "queued")
        return response.json()

    async def wait(self, run):
        job = self.runner.jobs.get((STORE_ID, run["id"]))
        if job is not None:
            await asyncio.wait_for(asyncio.shield(job), 5)
        return self.runner.store.get(STORE_ID, run["id"])

    async def test_success_contract_usage_and_session_credentials(self):
        run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(run["usage"], {"input_tokens": 30, "output_tokens": 15, "model_calls": 3, "unknown_calls": 0})
        self.assertEqual((run["lead_model"], run["worker_model"]), ("gpt-5", "gpt-5-mini"))
        self.assertEqual(run["tasks"][0]["attempts"], 1)
        self.assertTrue(run["final_output"].endswith(models.LIMITATION))
        self.assertEqual((await self.client.get(self.url + "/" + run["id"])).json(), run)
        self.assertEqual((await self.client.get(self.url)).json(), {"runs": [run]})
        self.assertNotIn(SECRET, self.runner.store.path.read_text())
        self.assertNotIn("api_key", self.runner.store.path.read_text())

    async def test_dag_workers_use_completed_dependency_outputs(self):
        self.plan["tasks"] = [planned("c", ["b"]), planned("b", ["a"]), planned("a")]
        run = await self.wait(await self.submit())
        workers = [payload for _, prompt, payload in self.requests if prompt == task_runner.WORKER]
        self.assertEqual([p["task"]["id"] for p in workers], ["a", "b", "c"])
        self.assertEqual(workers[1]["dependencies"], {"a": "Output a"})
        self.assertEqual(workers[2]["dependencies"], {"b": "Output b"})
        self.assertEqual(run["status"], "completed")

    async def test_revision_invalidates_transitive_descendants_only(self):
        self.plan["tasks"] = [planned(), planned("b", ["a"]), planned("c", ["b"]), planned("d")]
        self.reviews = [review(False, ["a"]), review()]
        run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(run["revision_count"], 1)
        self.assertEqual({t["id"]: t["attempts"] for t in run["tasks"]}, {"a": 2, "b": 2, "c": 2, "d": 1})
        revised = [p for _, prompt, p in self.requests if prompt == task_runner.WORKER and p["review_feedback"]]
        self.assertEqual({p["task"]["id"] for p in revised}, {"a", "b", "c"})
        self.assertTrue(all(p["previous_output"] for p in revised))

    async def test_maximum_rounds_and_37_call_cap(self):
        self.plan["tasks"] = [planned(str(n)) for n in range(8)]
        self.reviews = [review(False, [str(n) for n in range(8)]) for _ in range(4)]
        run = await self.wait(await self.submit(max_tasks=8, max_revisions=3))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["usage"]["model_calls"], 37)
        self.assertEqual(run["revision_count"], 3)
        self.assertEqual(len(run["reviews"]), 4)
        self.assertTrue(all(t["attempts"] == 4 and t["output"] for t in run["tasks"]))

    async def test_explicit_call_cap_and_zero_revisions(self):
        with patch.object(task_runner, "CALL_LIMIT", 1):
            run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "failed")
        self.assertIn("cap", run["error"])
        self.assertEqual(run["usage"]["model_calls"], 1)
        self.reviews = [review(False, ["a"])]
        run = await self.wait(await self.submit(max_revisions=0))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["revision_count"], 0)
        self.assertEqual(run["tasks"][0]["output"], "Output a")

    async def test_invalid_plan_and_review_fail_without_retries(self):
        self.plan["tasks"] = [planned("a", ["a"])]
        run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "failed")
        self.assertIn("invalid plan", run["error"])
        self.assertEqual(run["usage"]["model_calls"], 1)
        self.plan["tasks"] = [planned()]
        self.reviews = [review(True, ["unknown"])]
        run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "failed")
        self.assertIn("invalid review", run["error"])
        self.assertEqual(run["usage"]["model_calls"], 3)

    async def test_provider_error_usage_redaction_and_no_fallback(self):
        self.gateway.side_effect = models.InferenceError("Denied " + SECRET, {"input_tokens": 9, "output_tokens": 2})
        run = await self.wait(await self.submit(goal="Draft " + SECRET))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["usage"], {"input_tokens": 9, "output_tokens": 2, "model_calls": 1, "unknown_calls": 0})
        self.assertNotIn(SECRET, json.dumps(run))
        self.assertNotIn(SECRET, self.runner.store.path.read_text())
        self.assertEqual(self.gateway.await_count, 1)

    async def test_output_redaction_and_unexpected_error(self):
        self.reviews = [{**review(), "feedback": SECRET, "final_output": SECRET}]
        run = await self.wait(await self.submit())
        self.assertNotIn(SECRET, json.dumps(run))
        self.gateway.side_effect = RuntimeError(SECRET)
        run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "failed")
        self.assertNotIn(SECRET, json.dumps(run))
        self.assertIn("unexpectedly", run["error"])

    async def test_unicode_escaped_credentials_redacted_after_json_decode(self):
        self.plan["tasks"][0]["instructions"] = SECRET
        self.reviews = [{**review(), "feedback": SECRET, "final_output": SECRET}]
        async def escaped(*args):
            result = await self.infer(*args)
            result["text"] = result["text"].replace(SECRET, "".join(f"\\u{ord(c):04x}" for c in SECRET))
            return result
        self.gateway.side_effect = escaped
        run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "completed")
        self.assertNotIn(SECRET, json.dumps(run))
        self.assertEqual(run["tasks"][0]["instructions"], "[REDACTED]")
        self.assertNotIn(SECRET, self.runner.store.path.read_text())

    async def test_discovery_errors_and_empty_list_fail_without_inference(self):
        self.discovery.side_effect = providers.ProviderError(401, "Denied " + SECRET)
        response = await self.client.post(self.url + "/model-options", json={"provider": "openai"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(SECRET, response.text)
        run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "failed")
        self.assertNotIn(SECRET, json.dumps(run))
        self.discovery.side_effect = None
        self.discovery.return_value = discovered()
        run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "failed")
        self.gateway.assert_not_awaited()

    async def test_anthropic_workflow(self):
        self.discovery.return_value = discovered("claude-opus-4-1", "claude-haiku-4-5")
        run = await self.wait(await self.submit(provider="anthropic"))
        self.assertEqual(run["status"], "completed")
        self.assertEqual((run["lead_model"], run["worker_model"]), ("claude-opus-4-1", "claude-haiku-4-5"))
        self.assertTrue(all(call.args[0] == "anthropic" for call in self.gateway.await_args_list))

    async def test_discovered_overrides_only_and_no_fallback(self):
        for overrides in ({"lead_model": "gpt-invented"}, {"worker_model": "gpt-5-codex"}):
            run = await self.wait(await self.submit(**overrides))
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["usage"]["model_calls"], 0)
        run = await self.wait(await self.submit(lead_model="gpt-5-mini", worker_model="gpt-5"))
        self.assertEqual(run["status"], "completed")
        self.assertEqual((run["lead_model"], run["worker_model"]), ("gpt-5-mini", "gpt-5"))

    async def test_incompatible_explicit_overrides_fail_before_any_paid_call(self):
        incompatible = ("o1-mini", "o1-mini-2024-09-12", "o1-preview", "gpt-3.5-turbo-0125", "gpt-4-32k")
        self.discovery.return_value = discovered("gpt-5", "gpt-5-mini", *incompatible)
        for model in incompatible:
            for role in ("lead_model", "worker_model"):
                with self.subTest(model=model, role=role):
                    run = await self.wait(await self.submit(**{role: model}))
                    self.assertEqual(run["status"], "failed")
                    self.assertEqual(run["usage"]["model_calls"], 0)
        self.gateway.assert_not_awaited()

    async def test_model_options_no_inference_and_missing_keys(self):
        response = await self.client.post(self.url + "/model-options", json={"provider": "openai"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {"models", "lead_model", "worker_model", "reason"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.gateway.assert_not_awaited()
        self.settings._sessions.clear()
        for url, body in ((self.url, {"goal": "Draft", "provider": "openai"}),
                          (self.url + "/model-options", {"provider": "openai"})):
            response = await self.client.post(url, json=body)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("credential", response.json()["detail"])
        self.assertEqual(self.runner.store.list(STORE_ID), [])

    async def test_unknown_provider_secret_fields_and_uuid_validation(self):
        for body in ({"goal": "Draft", "provider": "copilot"}, {"goal": "Draft", "provider": "openai", "api_key": SECRET},
                     {"goal": SECRET, "provider": "openai", "max_tasks": 9}):
            response = await self.client.post(self.url, json=body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(SECRET, response.text)
        for url in (self.url + "/" + SECRET, "/api/stores/not-a-uuid/task-runs"):
            response = await self.client.get(url)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(SECRET, response.text)

    async def test_cross_store_and_deleted_store_routes(self):
        run = await self.wait(await self.submit())
        other = f"/api/stores/{OTHER_STORE}/task-runs"
        self.assertEqual((await self.client.get(other)).json(), {"runs": []})
        self.assertEqual((await self.client.get(other + "/" + run["id"])).status_code, 404)
        self.assertEqual((await self.client.post(other + "/" + run["id"] + "/cancel", json={})).status_code, 404)
        self.existing.clear()
        for method, url, kwargs in (("GET", self.url, {}), ("GET", self.url + "/" + run["id"], {}),
                                    ("POST", self.url, {"json": {"goal": "Draft", "provider": "openai"}}),
                                    ("POST", self.url + "/model-options", {"json": {"provider": "openai"}}),
                                    ("POST", self.url + "/" + run["id"] + "/cancel", {"json": {}})):
            self.assertEqual((await self.client.request(method, url, **kwargs)).status_code, 404)

    async def test_guards_before_body_parsing(self):
        for headers, expected in (({"Host": "evil.example"}, 403), ({"Origin": "http://evil.example"}, 403),
                                  ({"Origin": "null"}, 403), ({"X-Forwarded-For": "127.0.0.1"}, 403),
                                  ({"Content-Type": "text/plain"}, 415)):
            response = await self.client.post(self.url, content="not-json " + SECRET,
                                              headers={"Content-Type": "application/json", **headers})
            self.assertEqual(response.status_code, expected, response.text)
            self.assertNotIn(SECRET, response.text)
        response = await self.client.post(self.url, content="x" * 16385, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        for url in (self.url, self.url + "/" + str(uuid4())):
            self.assertEqual((await self.client.get(url, headers={"Origin": "https://evil.example"})).status_code, 403)
        transport = httpx.ASGITransport(app=self.app, client=("192.168.1.2", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as remote:
            self.assertEqual((await remote.get(self.url)).status_code, 403)
        self.assertEqual((await self.client.get(self.url, headers={"Origin": "http://localhost:8000"})).status_code, 200)

    async def test_queued_cancel_and_admission_bound(self):
        await self.runner.calls.acquire()
        await self.runner.calls.acquire()
        try:
            runs = [await self.submit() for _ in range(20)]
            response = await self.client.post(self.url, json={"goal": "Draft", "provider": "openai"})
            self.assertEqual(response.status_code, 429)
            response = await self.client.post(self.url + "/" + runs[0]["id"] + "/cancel", json={})
            self.assertEqual(response.json()["status"], "cancelled")
            self.gateway.assert_not_awaited()
            self.discovery.assert_not_awaited()
            await self.runner.shutdown()
        finally:
            self.runner.calls.release()
            self.runner.calls.release()
        cancelled = self.runner.store.get(STORE_ID, runs[0]["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(all(self.runner.store.get(STORE_ID, r["id"])["status"] == "interrupted" for r in runs[1:]))

    async def test_inflight_cancel_stops_http_and_is_immutable(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def blocked(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.gateway.side_effect = blocked
        run = await self.submit()
        await asyncio.wait_for(started.wait(), 3)
        response = await self.client.post(self.url + "/" + run["id"] + "/cancel", json={})
        self.assertTrue(stopped.is_set())
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(response.json()["usage"]["unknown_calls"], 1)
        self.assertEqual((await self.client.post(self.url + "/" + run["id"] + "/cancel", json={})).json(), response.json())
        self.assertIsNone(self.runner.store.update(STORE_ID, run["id"], lambda r: r.update(status="completed")))

    async def test_worker_cancellation_cascades_to_both_http_calls(self):
        self.plan["tasks"] = [planned(), planned("b"), planned("c", ["a"])]
        started, stopped = set(), set()
        both = asyncio.Event()
        async def blocked(*args):
            if args[-2] != task_runner.WORKER:
                return await self.infer(*args)
            task_id = args[-1]["task"]["id"]
            started.add(task_id)
            if len(started) == 2:
                both.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(task_id)
        self.gateway.side_effect = blocked
        run = await self.submit()
        await asyncio.wait_for(both.wait(), 3)
        response = await self.client.post(self.url + "/" + run["id"] + "/cancel", json={})
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(stopped, {"a", "b"})
        self.assertEqual(self.gateway.await_count, 3)
        self.assertEqual([t["status"] for t in response.json()["tasks"]], ["failed", "failed", "pending"])

    async def test_worker_error_cancels_sibling_and_retains_usage(self):
        self.plan["tasks"] = [planned(), planned("b")]
        sibling_started, sibling_stopped = asyncio.Event(), asyncio.Event()
        async def failing(*args):
            if args[-2] != task_runner.WORKER:
                return await self.infer(*args)
            if args[-1]["task"]["id"] == "a":
                await sibling_started.wait()
                raise models.InferenceError("Worker denied.", {"input_tokens": 2, "output_tokens": 1})
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                sibling_stopped.set()
        self.gateway.side_effect = failing
        run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "failed")
        self.assertTrue(sibling_stopped.is_set())
        self.assertEqual(run["usage"], {"input_tokens": 12, "output_tokens": 6, "model_calls": 3, "unknown_calls": 1})
        self.assertTrue(all(t["status"] == "failed" for t in run["tasks"]))

    async def test_late_cancelled_response_cannot_complete(self):
        started = asyncio.Event()
        async def stubborn(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {"text": json.dumps(self.plan), "input_tokens": 1, "output_tokens": 1}
        self.gateway.side_effect = stubborn
        run = await self.submit()
        await asyncio.wait_for(started.wait(), 3)
        response = await self.client.post(self.url + "/" + run["id"] + "/cancel", json={})
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(self.runner.store.get(STORE_ID, run["id"]), response.json())
        self.assertEqual(self.gateway.await_count, 1)

    async def test_cancel_write_failure_stops_and_fences_live_inference(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def stubborn(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {"text": json.dumps(self.plan), "input_tokens": 7, "output_tokens": 3}
            finally:
                stopped.set()
        self.gateway.side_effect = stubborn
        run = await self.submit()
        await asyncio.wait_for(started.wait(), 3)
        other_url = f"/api/stores/{OTHER_STORE}/task-runs/{run['id']}/cancel"
        self.assertEqual((await self.client.post(other_url, json={})).status_code, 404)
        self.assertFalse(stopped.is_set())
        with patch.object(self.runner.store, "finish", side_effect=providers.ProviderError(503, "Disk " + SECRET)):
            response = await self.client.post(self.url + "/" + run["id"] + "/cancel", json={})
        self.assertEqual(response.status_code, 503)
        self.assertIn("could not be persisted", response.json()["detail"])
        self.assertNotIn(SECRET, response.text)
        self.assertTrue(stopped.is_set())
        persisted = self.runner.store.get(STORE_ID, run["id"])
        self.assertEqual(persisted["status"], "planning")
        self.assertEqual(persisted["tasks"], [])
        self.assertEqual(persisted["usage"], {"input_tokens": 7, "output_tokens": 3, "model_calls": 1, "unknown_calls": 0})
        self.assertEqual(self.gateway.await_count, 1)
        self.assertNotIn((STORE_ID, run["id"]), self.runner.jobs)
        with self.assertRaises(asyncio.CancelledError):
            await self.runner._active(STORE_ID, run["id"])

    async def test_cancel_error_response_still_accounts_late_usage(self):
        started = asyncio.Event()
        async def late_error(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise models.InferenceError("Provider " + SECRET, {"input_tokens": 6, "output_tokens": 2}) from None
        self.gateway.side_effect = late_error
        run = await self.submit()
        await asyncio.wait_for(started.wait(), 3)
        response = await self.client.post(self.url + "/" + run["id"] + "/cancel", json={})
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(response.json()["usage"], {"input_tokens": 6, "output_tokens": 2, "model_calls": 1, "unknown_calls": 0})
        self.assertNotIn(SECRET, response.text)

    async def test_cancel_request_cancellation_still_finishes_cleanup(self):
        started, stopped, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        async def blocked(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.gateway.side_effect = blocked
        run = await self.submit()
        await asyncio.wait_for(started.wait(), 3)
        finish = self.runner.store.finish
        def blocked_finish(*args):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise AssertionError("Cancellation barrier timed out")
            return finish(*args)
        with patch.object(self.runner.store, "finish", side_effect=blocked_finish):
            request = asyncio.create_task(self.client.post(self.url + "/" + run["id"] + "/cancel", json={}))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                request.cancel()
                await asyncio.sleep(0)
                request.cancel()
                await asyncio.sleep(0)
                self.assertFalse(request.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(request, 3)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.runner.store.get(STORE_ID, run["id"])["status"], "cancelled")
        self.assertNotIn((STORE_ID, run["id"]), self.runner.jobs)

    async def test_shutdown_write_failure_still_stops_and_releases_owner(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def stubborn(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {"text": json.dumps(self.plan), "input_tokens": 2, "output_tokens": 1}
            finally:
                stopped.set()
        self.gateway.side_effect = stubborn
        run = await self.submit()
        await asyncio.wait_for(started.wait(), 3)
        with patch.object(self.runner.store, "interrupt_active", side_effect=providers.ProviderError(503, "Disk unavailable.")):
            with self.assertRaises(providers.ProviderError):
                await self.runner.shutdown()
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.runner.ready)
        self.assertIsNone(self.runner.store._owner)
        self.assertEqual(self.gateway.await_count, 1)
        self.assertEqual(self.runner.store.get(STORE_ID, run["id"])["status"], "planning")
        self.assertFalse(self.runner.jobs)

    async def test_cancelled_submission_finishes_commit_and_registration(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        create = self.runner.store.create
        def blocked_create(*args):
            run = create(*args)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise AssertionError("Create barrier timed out")
            return run
        await self.runner.calls.acquire()
        await self.runner.calls.acquire()
        try:
            with patch.object(self.runner.store, "create", side_effect=blocked_create):
                request = asyncio.create_task(self.client.post(self.url, json={"goal": "Draft", "provider": "openai"}))
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    request.cancel()
                    await asyncio.sleep(0)
                    request.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(request.done())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(request, 3)
            run = self.runner.store.list(STORE_ID)[0]
            self.assertIn((STORE_ID, run["id"]), self.runner.jobs)
            self.gateway.assert_not_awaited()
            await self.runner.shutdown()
            self.assertEqual(self.runner.store.get(STORE_ID, run["id"])["status"], "interrupted")
            self.assertFalse(self.runner.jobs)
        finally:
            release.set()
            self.runner.calls.release()
            self.runner.calls.release()

    async def test_startup_cancellation_settles_thread_before_releasing_owner(self):
        await self.runner.shutdown()
        for method in ("acquire_owner", "interrupt_active"):
            with self.subTest(method=method):
                entered, release = asyncio.Event(), threading.Event()
                loop = asyncio.get_running_loop()
                original = getattr(self.runner.store, method)
                def blocked():
                    loop.call_soon_threadsafe(entered.set)
                    if not release.wait(3):
                        raise AssertionError("Startup barrier timed out")
                    return original()
                with patch.object(self.runner.store, method, side_effect=blocked):
                    startup = asyncio.create_task(self.runner.startup())
                    try:
                        await asyncio.wait_for(entered.wait(), 3)
                        startup.cancel()
                        await asyncio.sleep(0)
                        startup.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(startup.done())
                    finally:
                        release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(startup, 3)
                self.assertFalse(self.runner.ready)
                self.assertIsNone(self.runner.store._owner)
                other = TaskStore(self.runner.store.path)
                other.acquire_owner()
                other.release_owner()

    async def test_shutdown_releases_owner_even_when_not_ready(self):
        await self.runner.shutdown()
        self.runner.store.acquire_owner()
        self.assertFalse(self.runner.ready)
        await self.runner.shutdown()
        self.assertIsNone(self.runner.store._owner)
        other = TaskStore(self.runner.store.path)
        other.acquire_owner()
        other.release_owner()

    async def test_unknown_usage_is_not_reported_as_known_zero(self):
        async def partial(*args):
            result = await self.infer(*args)
            result.pop("output_tokens")
            return result
        self.gateway.side_effect = partial
        run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["usage"], {"input_tokens": 30, "output_tokens": 0, "model_calls": 3, "unknown_calls": 3})
        self.gateway.side_effect = models.InferenceError("Provider failed without usage.")
        run = await self.wait(await self.submit())
        self.assertEqual(run["usage"]["unknown_calls"], 1)

    async def test_startup_shutdown_and_owner_failure_do_not_replay(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def blocked(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.gateway.side_effect = blocked
        run = await self.submit()
        await asyncio.wait_for(started.wait(), 3)
        other = task_runner.TaskRunner(TaskStore(self.runner.store.path))
        with self.assertRaises(providers.ProviderError):
            await other.startup()
        self.assertEqual(self.runner.store.get(STORE_ID, run["id"])["status"], "planning")
        await self.runner.shutdown()
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.runner.store.get(STORE_ID, run["id"])["status"], "interrupted")
        stale = self.runner.store.create(STORE_ID, models.RunInput(goal="Draft", provider="openai").model_dump())
        await self.runner.startup()
        self.assertEqual(self.runner.store.get(STORE_ID, stale["id"])["status"], "interrupted")
        self.assertEqual(self.gateway.await_count, 1)

    async def test_shutdown_late_result_stays_interrupted(self):
        started = asyncio.Event()
        async def stubborn(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {"text": json.dumps(self.plan), "input_tokens": 1, "output_tokens": 1}
        self.gateway.side_effect = stubborn
        run = await self.submit()
        await asyncio.wait_for(started.wait(), 3)
        await self.runner.shutdown()
        self.assertEqual(self.runner.store.get(STORE_ID, run["id"])["status"], "interrupted")
        self.assertEqual(self.runner.store.get(STORE_ID, run["id"])["usage"],
                         {"input_tokens": 1, "output_tokens": 1, "model_calls": 1, "unknown_calls": 0})
        self.assertEqual(self.gateway.await_count, 1)

    async def test_deleted_store_stops_before_next_paid_call(self):
        async def deleted(*args):
            result = await self.infer(*args)
            self.existing.clear()
            return result
        self.gateway.side_effect = deleted
        run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "failed")
        self.assertIn("Store not found", run["error"])
        self.assertEqual(self.gateway.await_count, 1)

    async def test_call_and_run_timeouts(self):
        async def blocked(*args):
            await asyncio.Event().wait()
        self.gateway.side_effect = blocked
        with patch.object(task_runner, "CALL_TIMEOUT", 0.03):
            run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "failed")
        self.assertIn("timed out", run["error"])
        with patch.object(task_runner, "RUN_TIMEOUT", 0.03):
            run = await self.wait(await self.submit())
        self.assertEqual(run["status"], "failed")
        self.assertIn("total timeout", run["error"])

    async def test_discovery_admission_timeout_and_shutdown(self):
        async def blocked(*args):
            await asyncio.Event().wait()
        self.discovery.side_effect = blocked
        with patch.object(task_runner, "CALL_TIMEOUT", 0.03):
            response = await self.client.post(self.url + "/model-options", json={"provider": "openai"})
        self.assertEqual(response.status_code, 504)
        jobs = [asyncio.create_task(self.runner.options(STORE_ID, "openai")) for _ in range(20)]
        await asyncio.sleep(0)
        try:
            with self.assertRaises(providers.ProviderError) as error:
                await self.runner.options(STORE_ID, "openai")
            self.assertEqual(error.exception.status, 429)
            await self.runner.shutdown()
            self.assertTrue(all(job.cancelled() for job in jobs))
            self.assertFalse(self.runner.discoveries)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def test_global_call_and_per_run_worker_concurrency(self):
        self.plan["tasks"] = [planned(str(n)) for n in range(4)]
        self.reviews = [review() for _ in range(3)]
        active = peak = 0
        per_goal, peaks = {}, {}
        async def concurrent(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            goal = args[-1]["goal"]
            is_worker = args[-2] == task_runner.WORKER
            if is_worker:
                per_goal[goal] = per_goal.get(goal, 0) + 1
                peaks[goal] = max(peaks.get(goal, 0), per_goal[goal])
            try:
                await asyncio.sleep(0.01)
                return await self.infer(*args)
            finally:
                active -= 1
                if is_worker:
                    per_goal[goal] -= 1
        self.gateway.side_effect = concurrent
        runs = [await self.submit(goal=f"Draft {i}") for i in range(3)]
        finished = await asyncio.gather(*(self.wait(run) for run in runs))
        self.assertTrue(all(run["status"] == "completed" for run in finished))
        self.assertEqual(peak, 2)
        self.assertTrue(all(count <= 2 for count in peaks.values()))

    async def test_existing_api_routes_and_lifespan_integration(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, client=("127.0.0.1", 1234)), base_url="http://localhost:8000") as client:
            with patch.object(storage, "list_stores", return_value=[{"id": STORE_ID}]):
                self.assertEqual((await client.get("/api/stores")).json(), [{"id": STORE_ID}])
            self.assertEqual((await client.get("/api/providers")).status_code, 200)
            self.assertEqual((await client.get(self.url)).json(), {"runs": []})
        await self.runner.shutdown()
        async with api.app.router.lifespan_context(api.app):
            self.assertTrue(self.runner.ready)
        self.assertFalse(self.runner.ready)


class InferenceTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, provider="openai", payload=None, status=200, headers=None):
        payload = payload or {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Draft " + SECRET}]}],
                              "usage": {"input_tokens": 3, "output_tokens": 2}}
        real_client = httpx.AsyncClient
        self.requests = []
        def handle(request):
            self.requests.append(request)
            return httpx.Response(status, json=payload, headers=headers)
        def factory(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return real_client(transport=httpx.MockTransport(handle), **kwargs)
        with patch.object(models.httpx, "AsyncClient", side_effect=factory), patch.object(models.tiktoken, "get_encoding") as encoding:
            encoding.return_value.encode.return_value = [0]
            return await models.infer(provider, SECRET, "gpt-5" if provider == "openai" else "claude-sonnet-4", "Draft", {"goal": "Draft"})

    async def test_openai_privacy_fixed_url_usage_and_redaction(self):
        result = await self.call()
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (3, 2))
        self.assertNotIn(SECRET, result["text"])
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
        body = json.loads(request.content)
        self.assertIs(body["store"], False)
        self.assertNotIn("tools", body)
        self.assertNotIn(SECRET, request.content.decode())

    async def test_anthropic_text_protocol(self):
        result = await self.call("anthropic", {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Draft"}], "usage": {"input_tokens": 4, "output_tokens": 1}})
        self.assertEqual(result["text"], "Draft")
        self.assertEqual(str(self.requests[0].url), "https://api.anthropic.com/v1/messages")
        self.assertNotIn("tools", json.loads(self.requests[0].content))

    async def test_missing_upstream_usage_and_exact_output_limit(self):
        result = await self.call(payload={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "x" * models.MAX_OUTPUT_CHARS}]}]})
        self.assertEqual(len(result["text"]), models.MAX_OUTPUT_CHARS)
        self.assertNotIn("input_tokens", result)
        self.assertNotIn("output_tokens", result)
        self.assertIs(json.loads(self.requests[0].content)["store"], False)

    async def test_errors_incomplete_refusal_and_usage(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), self.assertRaises(models.InferenceError) as error:
                await self.call(status=status, payload={"error": SECRET, "usage": {"input_tokens": 8, "output_tokens": 1}})
            self.assertNotIn(SECRET, str(error.exception))
            self.assertEqual(error.exception.usage["input_tokens"], 8)
        for payload in ({"status": "incomplete", "usage": {"input_tokens": 7}},
                        {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": SECRET}]}]},
                        {"status": "completed", "output": []}, {"usage": {"input_tokens": -1}},
                        {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "x" * (models.MAX_OUTPUT_CHARS + 1)}]}]}):
            with self.subTest(payload=str(payload)[:80]), self.assertRaises(models.InferenceError) as error:
                await self.call(payload=payload)
            self.assertNotIn(SECRET, str(error.exception))
        with self.assertRaises(models.InferenceError):
            await self.call("anthropic", {"stop_reason": "max_tokens", "content": []})

    async def test_quota_and_rate_limits_have_distinct_safe_diagnostics(self):
        for provider, code, expected in (
            ("openai", "insufficient_quota", "API quota/billing blocked"),
            ("openai", "billing_hard_limit_reached", "API quota/billing blocked"),
            ("openai", "rate_limit_exceeded", "API request/token rate limit"),
            ("anthropic", "rate_limit_error", "API request/token rate limit"),
        ):
            with self.subTest(provider=provider, code=code), self.assertRaises(models.InferenceError) as error:
                await self.call(provider, {"error": {"code": code, "type": code, "message": SECRET},
                                           "usage": {"input_tokens": 8, "output_tokens": 1}}, 429,
                                {"retry-after": "12"})
            self.assertIn(expected, str(error.exception))
            self.assertNotIn(SECRET, str(error.exception))
            self.assertEqual(error.exception.usage, {"input_tokens": 8, "output_tokens": 1})
            self.assertEqual(len(self.requests), 1)
            if "rate_limit" in code:
                self.assertIn("at least 12 seconds", str(error.exception))
            else:
                self.assertNotIn("at least 12 seconds", str(error.exception))

    async def test_unknown_429_does_not_guess_or_echo_upstream_error(self):
        with self.assertRaises(models.InferenceError) as error:
            await self.call(payload={"error": {"code": SECRET, "type": [], "message": SECRET}}, status=429,
                            headers={"retry-after": SECRET})
        self.assertIn("without a recognized reason", str(error.exception))
        self.assertNotIn(SECRET, str(error.exception))
