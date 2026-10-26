"""Routing integration tests: isolated settings, temporary persistence, no upstream I/O."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import provider_api
import provider_transport
import providers
import storage
import task_api
import task_models as models
import task_routing as routing
import task_runner
from task_store import TaskStore


LOCAL = "profile_" + "a" * 32
REMOTE = "profile_" + "b" * 32
OTHER = "profile_" + "c" * 32
STORE = str(uuid4())
SECRET = "remote-private-test-credential"
LOCAL_SECRET = "local-private-test-credential"
FIXED_INFER = models.infer
CUSTOM_INFER = provider_transport.infer
CUSTOM_DISCOVER = provider_transport.discover
HTTP_CLIENT = httpx.AsyncClient


def task(identifier="a", **route):
    return {"id": identifier, "title": "Draft", "instructions": "Explain the result.",
            "acceptance_criteria": ["State assumptions"], "depends_on": [], **route}


def discovered(*names):
    return {"models": [{"id": name, "name": name} for name in names]}


class FakeSettings:
    def __init__(self):
        self.pref = {"mode": "local_only", "providers": [LOCAL, REMOTE, "openai"]}
        self.resolved = []
        self.connections = {
            LOCAL: {"id": LOCAL, "kind": "ollama", "base_url": "http://127.0.0.1:11434",
                    "is_local": True, "api_key": None, "context_tokens": 8192,
                    "output_tokens": 2048, "requires_key": False},
            REMOTE: {"id": REMOTE, "kind": "openai_compatible", "base_url": "https://example.invalid/v1",
                     "is_local": False, "api_key": SECRET, "context_tokens": 8192,
                     "output_tokens": 2048, "requires_key": True},
            "openai": {"id": "openai", "kind": "openai", "base_url": "https://api.openai.com/v1",
                       "is_local": False, "api_key": SECRET, "context_tokens": 8192,
                       "output_tokens": 2048, "requires_key": True},
        }
        self.profiles = {key: {"id": key, "name": key, "kind": value["kind"],
                        "is_local": value["is_local"], "default_model": None,
                        **{field: value[field] for field in ("base_url", "requires_key", "context_tokens", "output_tokens")}}
                         for key, value in self.connections.items()}
        for identifier in (LOCAL, REMOTE):
            self.connections[identifier]["allowed_models"] = []
            self.profiles[identifier]["allowed_models"] = []

    def routing(self):
        return deepcopy(self.pref)

    def status(self):
        raise AssertionError("Task routing must not resolve all credentials through status().")

    def catalog(self):
        return deepcopy(list(self.profiles.values()))

    def connection(self, identifier):
        self.resolved.append(identifier)
        if identifier not in self.connections:
            raise providers.ProviderError(404, "Unknown profile " + SECRET)
        if self.connections[identifier]["requires_key"] and not self.connections[identifier]["api_key"]:
            raise providers.ProviderError(422, "Selected credential is unavailable.")
        return deepcopy(self.connections[identifier])

    def resolve(self, identifier):
        return self.connection(identifier)["api_key"], "session"


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = FakeSettings()
        self.runner = task_runner.TaskRunner(TaskStore(Path(temporary.name) / "tasks.json"))
        self.catalogs = {LOCAL: discovered("strange-draft:70b", "my-custom:8b", "shared"),
                         REMOTE: discovered("remote-pro:72b", "shared"),
                         "openai": discovered("gpt-5", "gpt-5-mini")}
        self.plan = {"tasks": [task()]}
        self.requests = []
        self.after_plan = None
        self.discovery = AsyncMock(side_effect=self.discover)
        self.inference = AsyncMock(side_effect=self.infer)
        self.fixed_discovery = AsyncMock(side_effect=lambda provider, _: deepcopy(self.catalogs[provider]))
        self.fixed_inference = AsyncMock(side_effect=self.infer_fixed)
        for patcher in (
            patch.object(provider_api, "settings", self.settings),
            patch.object(storage, "get_store", return_value={"id": STORE}),
            patch.object(provider_transport, "discover", self.discovery),
            patch.object(provider_transport, "infer", self.inference),
            patch.object(providers, "list_models", self.fixed_discovery),
            patch.object(models, "infer", self.fixed_inference),
            patch.object(models.tokenization, "get_encoding"),
            patch.object(task_runner, "runner", self.runner),
        ):
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            if patcher.attribute == "get_encoding":
                mocked.return_value.encode.return_value = [0]
        await self.runner.startup()
        self.addAsyncCleanup(self.runner.shutdown)
        app = FastAPI()
        app.include_router(task_api.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4321)),
                                        base_url="http://localhost:8000")
        self.addAsyncCleanup(self.client.aclose)
        self.url = f"/api/stores/{STORE}/task-runs"

    async def discover(self, connection):
        result = deepcopy(self.catalogs[connection["id"]])
        for item in result["models"]:
            item.setdefault("is_local", connection["is_local"])
        return result

    async def infer_fixed(self, provider, credential, model, instructions, payload, *, before_send=None):
        return await self.infer({"id": provider}, model, instructions, payload, before_send=before_send)

    async def infer(self, connection, model, instructions, payload, *, before_send=None):
        if before_send is not None:
            connection = await before_send()
        self.requests.append((connection["id"], model, instructions, deepcopy(payload)))
        if instructions.startswith(task_runner.PLANNER):
            text = json.dumps(self.plan)
            if self.after_plan:
                self.after_plan()
        elif instructions.startswith(task_runner.REVIEWER):
            text = json.dumps({"approved": True, "feedback": "Accepted", "revision_tasks": [],
                               "final_output": "Synthesized draft " + SECRET + " " + LOCAL_SECRET,
                               "task_reviews": [{"task_id": t["id"], "verdict": "keep", "reason": "Meets criteria", "change": None} for t in payload["tasks"]]})
        else:
            text = "Worker draft " + SECRET + " " + LOCAL_SECRET
        return {"text": text, "input_tokens": 4, "output_tokens": 2}

    async def submit(self, **overrides):
        response = await self.client.post(self.url, json={"provider": "auto", "goal": "Draft", **overrides})
        self.assertEqual(response.status_code, 202, response.text)
        run = response.json()
        job = self.runner.jobs.get((STORE, run["id"]))
        if job:
            await asyncio.wait_for(asyncio.shield(job), 5)
        return self.runner.store.get(STORE, run["id"])

    async def options(self, provider="auto"):
        response = await self.client.post(self.url + "/model-options", json={"provider": provider})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_shared_decisions_and_artifact_versions_reach_every_followup_stage(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE]}
        self.settings.connections[LOCAL]["api_key"] = LOCAL_SECRET
        async def with_decisions(*args, **kwargs):
            result = await self.infer(*args, **kwargs)
            if args[2].startswith(task_runner.REVIEWER):
                value = json.loads(result["text"])
                value["decisions"] = ["Keep the stable JSON interface"]
                result["text"] = json.dumps(value)
            return result
        self.inference.side_effect = with_decisions
        run = await self.submit()
        self.assertEqual(run["status"], "completed", run.get("error"))
        start = len(self.requests)
        updated = await self.followup(run)
        self.assertEqual(updated["status"], "completed", updated.get("error"))
        self.assertEqual(updated["id"], run["id"])
        self.assertEqual(updated["turns"][0]["tasks"][0]["output_versions"], run["tasks"][0]["output_versions"])
        for _, _, _, payload in self.requests[start:]:
            memory = payload["collaboration_memory"]
            self.assertEqual(memory["decisions"][0]["decision"], "Keep the stable JSON interface")
            self.assertEqual(memory["prior_work"][0]["turn_id"], run["turn_id"])
            self.assertEqual(memory["prior_work"][0]["version"], 1)
            self.assertNotIn(SECRET, json.dumps(memory))
            self.assertNotIn(LOCAL_SECRET, json.dumps(memory))

    async def followup(self, run, **overrides):
        body = {"goal": "Add a second example", "provider": run["provider"], "mode": run.get("mode", "delegate"),
                "message_id": str(uuid4()), "expected_turn_id": run.get("turn_id", run["id"]), **overrides}
        response = await self.client.post(f"{self.url}/{run['id']}/messages", json=body)
        self.assertEqual(response.status_code, 202, response.text)
        job = self.runner.jobs.get((STORE, run["id"]))
        if job:
            await asyncio.wait_for(asyncio.shield(job), 5)
        return self.runner.store.get(STORE, run["id"])

    async def test_followups_keep_one_task_and_send_previous_answers(self):
        first = await self.submit(provider=LOCAL, mode="direct", goal="Create a reading list API")
        second = await self.followup(first, goal="Add pagination")
        third = await self.followup(second, goal="Now add filtering")
        self.assertEqual(third["status"], "completed", third)
        self.assertEqual(first["id"], third["id"])
        self.assertEqual(len(self.runner.store.list(STORE)), 1)
        self.assertEqual(third["root_goal"], first["goal"])
        self.assertEqual(third["usage"]["model_calls"], 3)
        self.assertEqual(third["turn_start_usage"]["model_calls"], 2)
        self.assertEqual([turn["goal"] for turn in third["turns"]], [first["goal"], "Add pagination"])
        self.assertEqual([turn["usage"]["model_calls"] for turn in third["turns"]], [1, 1])
        history = self.requests[-1][3]["conversation"]
        self.assertEqual(history["original_goal"], first["goal"])
        self.assertEqual(history["turns"][0]["assistant"], first["final_output"])
        self.assertEqual(history["turns"][1]["user"], "Add pagination")
        self.assertIn("Build on that work", self.requests[-1][2])
        self.assertEqual(TaskStore(self.runner.store.path).get(STORE, first["id"])["turns"], third["turns"])

    async def test_delegated_followup_context_reaches_planner_workers_and_reviewer(self):
        first = await self.submit(provider=LOCAL)
        self.requests.clear()
        second = await self.followup(first)
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual(len(self.requests), 3)
        for request in self.requests:
            self.assertEqual(request[3]["conversation"]["turns"][0]["assistant"], first["final_output"])
        self.assertEqual(second["turns"][0]["tasks"], first["tasks"])

    async def test_followup_is_idempotent_even_for_concurrent_retries(self):
        first = await self.submit(provider=LOCAL, mode="direct")
        body = {"goal": "Improve it", "provider": LOCAL, "mode": "direct", "message_id": str(uuid4()), "expected_turn_id": first["turn_id"]}
        url = f"{self.url}/{first['id']}/messages"
        replies = await asyncio.gather(self.client.post(url, json=body), self.client.post(url, json=body))
        self.assertEqual([reply.status_code for reply in replies], [202, 202])
        job = self.runner.jobs.get((STORE, first["id"]))
        if job: await job
        self.assertEqual((await self.client.post(url, json=body)).status_code, 202)
        saved = self.runner.store.get(STORE, first["id"])
        self.assertEqual(saved["usage"]["model_calls"], 2)
        self.assertEqual(len(saved["turns"]), 1)
        self.assertEqual(saved["turn_id"], body["message_id"])
        changed = await self.client.post(url, json={**body, "goal": "Different instruction"})
        self.assertEqual(changed.status_code, 409)

    async def test_followup_blocks_stale_turns_and_cross_project_access(self):
        first = await self.submit(provider=LOCAL, mode="direct")
        await self.followup(first)
        body = {"goal": "Stale change", "provider": LOCAL, "mode": "direct", "message_id": str(uuid4()), "expected_turn_id": first["turn_id"]}
        self.assertEqual((await self.client.post(f"{self.url}/{first['id']}/messages", json=body)).status_code, 409)
        foreign = f"/api/stores/{uuid4()}/task-runs/{first['id']}/messages"
        self.assertEqual((await self.client.post(foreign, json=body)).status_code, 404)
        self.assertEqual(len(self.requests), 2)

    async def test_followup_can_continue_after_cancel_and_rejects_overlap(self):
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.inference.side_effect = blocked
        response = await self.client.post(self.url, json={"provider": LOCAL, "mode": "direct", "goal": "First message"})
        first = response.json()
        await asyncio.wait_for(entered.wait(), 2)
        body = {"goal": "Second message", "provider": LOCAL, "mode": "direct", "message_id": str(uuid4()), "expected_turn_id": first["turn_id"]}
        self.assertEqual((await self.client.post(f"{self.url}/{first['id']}/messages", json=body)).status_code, 409)
        await self.client.post(f"{self.url}/{first['id']}/cancel", json={})
        self.inference.side_effect = self.infer
        second = await self.followup(first)
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual(second["turns"][0]["status"], "cancelled")
        self.assertEqual(second["usage"]["model_calls"], 2)

    async def test_followups_get_a_fresh_call_budget_without_losing_usage(self):
        with patch.object(task_runner, "CALL_LIMIT", 1):
            first = await self.submit(provider=LOCAL, mode="direct")
            second = await self.followup(first)
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual(second["usage"]["model_calls"], 2)
        self.assertEqual(second["usage"]["input_tokens"], 8)
        self.assertEqual(second["usage"]["unknown_calls"], 0)

    async def test_failed_followup_preserves_last_success_and_survives_restart(self):
        first = await self.submit(provider=LOCAL, mode="direct")
        self.inference.side_effect = providers.ProviderError(401, "Authentication failed")
        failed = await self.followup(first)
        self.assertEqual(failed["status"], "failed", failed)
        self.assertEqual(failed["turns"][0]["final_output"], first["final_output"])
        await self.runner.shutdown()
        await self.runner.startup()
        self.inference.side_effect = self.infer
        third = await self.followup(failed, goal="Try with a simpler version")
        self.assertEqual(third["status"], "completed", third)
        history = self.requests[-1][3]["conversation"]["turns"]
        self.assertEqual(history[0]["assistant"], first["final_output"])
        self.assertEqual(history[1]["status"], "failed")

    async def test_legacy_run_can_continue_in_place(self):
        first = await self.submit(provider=LOCAL, mode="direct")
        with self.runner.store.transaction(write=True) as data:
            for field in ("turn_id", "turns", "root_goal"):
                data["runs"][0].pop(field)
        legacy = self.runner.store.get(STORE, first["id"])
        second = await self.followup(legacy)
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["turns"][0]["turn_id"], first["id"])

    async def test_followup_rechecks_permissions_and_redacts_history_for_new_connection(self):
        first = await self.submit(provider=LOCAL, mode="direct", goal="Draft " + SECRET)
        # A credential unknown to the original local-only run must be scrubbed before remote dispatch.
        second = await self.followup(first, provider=REMOTE, goal="Improve it")
        self.assertEqual(second["status"], "completed", second)
        for request in self.requests[1:]: self.assertNotIn(SECRET, json.dumps(request))
        self.assertNotIn(SECRET, self.runner.store.path.read_text())
        self.settings.pref = {"mode": "local_only", "providers": [REMOTE]}
        body = {"goal": "Next", "provider": "auto", "mode": "direct", "message_id": str(uuid4()), "expected_turn_id": second["turn_id"]}
        response = await self.client.post(f"{self.url}/{first['id']}/messages", json=body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.runner.store.get(STORE, first["id"])["turns"]), 1)

    async def test_completion_only_models_are_excluded_even_as_saved_defaults(self):
        self.catalogs[LOCAL] = discovered("gpt-3.5-turbo-instruct", "text-davinci-003", "text-embedding-3-small",
                                         "qwen2.5:7b-instruct", "opaque-embedding-alias")
        self.catalogs[LOCAL]["models"][-1]["chat_capable"] = False
        self.settings.profiles[LOCAL]["default_model"] = "gpt-3.5-turbo-instruct"
        for provider in (LOCAL, "auto"):
            options = await self.options(provider)
            self.assertEqual([item["id"] for item in options["models"]], ["qwen2.5:7b-instruct"])
            run = await self.submit(provider=provider)
            self.assertEqual(run["status"], "completed", run)
        self.assertEqual({request[1] for request in self.requests}, {"qwen2.5:7b-instruct"})

    async def test_direct_mode_makes_one_call_without_plan_or_review(self):
        for provider in (LOCAL, "auto", "openai"):
            self.requests.clear()
            run = await self.submit(provider=provider, mode="direct", include_project_context=True)
            self.assertEqual(run["status"], "completed", run)
            self.assertEqual(run["mode"], "direct")
            self.assertEqual(run["usage"]["model_calls"], 1)
            self.assertEqual(run["tasks"], [])
            self.assertEqual(run["reviews"], [])
            self.assertEqual(self.requests[0][2], task_runner.DIRECT)
            self.assertIn("project_context", self.requests[0][3])
            self.assertTrue(run["final_output"].endswith(task_runner.DIRECT_LIMITATION))

    async def test_direct_mode_pins_requested_model_and_never_substitutes(self):
        self.inference.side_effect = providers.ProviderError(502, "Model unavailable", kind="model_unavailable")
        run = await self.submit(provider=LOCAL, mode="direct", lead_model="shared")
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(run["usage"]["model_calls"], 1)
        self.assertEqual(run["lead_model"], "shared")
        self.assertTrue(run["error_action"])

    async def test_direct_mode_rejects_worker_and_unapproved_model(self):
        response = await self.client.post(self.url, json={"provider": LOCAL, "goal": "Draft", "mode": "direct", "worker_model": "shared"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.runner.store.list(STORE), [])
        run = await self.submit(provider=LOCAL, mode="direct", lead_model="not-approved")
        self.assertEqual(run["status"], "failed", run)
        self.inference.assert_not_awaited()

    async def test_direct_mode_cancellation_does_not_complete_or_review(self):
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.inference.side_effect = blocked
        response = await self.client.post(self.url, json={"provider": LOCAL, "goal": "Draft", "mode": "direct"})
        run = response.json()
        await asyncio.wait_for(entered.wait(), 2)
        response = await self.client.post(f"{self.url}/{run['id']}/cancel", json={})
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertIsNone(response.json()["final_output"])
        self.assertEqual(response.json()["reviews"], [])

    async def test_astra_direct_settings_and_explicit_limits_reach_gateway(self):
        self.catalogs[REMOTE] = discovered("gpt-6-astra")
        seen = []
        async def gateway(connection, model, instructions, payload, *, before_send=None, options=None):
            seen.append(options)
            return await self.infer(connection, model, instructions, payload, before_send=before_send)
        self.inference.side_effect = gateway
        run = await self.submit(provider=REMOTE, mode="direct", lead_model="gpt-6-astra")
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(run["inference_settings"]["timeout_seconds"], 300)
        self.assertEqual(run["inference_settings"]["reasoning_effort"], "low")
        run = await self.submit(provider=REMOTE, mode="direct", lead_model="gpt-6-astra", timeout_seconds=420,
                                output_tokens=1024, reasoning_effort="medium")
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(seen[-1], {"timeout_seconds": 420, "output_tokens": 1024, "reasoning_effort": "medium"})
        self.assertEqual(run["inference_settings"]["timeout_seconds"], 420)

    async def test_run_settings_cannot_exceed_connection_limits_or_guess_reasoning(self):
        for options in ({"output_tokens": 8192}, {"reasoning_effort": "high"}):
            run = await self.submit(provider=LOCAL, mode="direct", **options)
            self.assertEqual(run["status"], "failed", run)
            self.assertEqual(run["usage"]["model_calls"], 0)
        self.inference.assert_not_awaited()

    async def test_completion_only_catalog_fails_before_inference(self):
        self.catalogs[LOCAL] = discovered("gpt-3.5-turbo-instruct")
        run = await self.submit(provider=LOCAL)
        self.assertEqual(run["status"], "failed")
        self.assertIn("No chat-capable models", run["error"])
        self.inference.assert_not_awaited()

    async def test_task_rules_choose_specialists_on_single_and_multiple_connections(self):
        self.catalogs[LOCAL] = discovered("qwen2.5-coder:7b", "llama3.2:3b", "deepseek-r1:32b")
        self.plan = {"tasks": [task("code", title="Set up environment", instructions="Implement a Python API"),
                               task("extract", instructions="Extract the document titles"),
                               task("reason", instructions="Prove the theorem")]}
        for provider in (LOCAL, "auto"):
            run = await self.submit(provider=provider)
            self.assertEqual(run["status"], "completed", run)
            self.assertEqual([item["model"] for item in run["tasks"]],
                             ["qwen2.5-coder:7b", "llama3.2:3b", "deepseek-r1:32b"])
            self.assertTrue(all(item["routing_reason"] for item in run["tasks"]))
        self.assertTrue(all(request[3].get("worker_candidates") for request in self.requests
                            if request[2] == task_runner.PLANNER))

    async def test_recognized_worker_failure_recovers_and_accounts_actual_route(self):
        self.plan = {"tasks": [task(model="shared", provider=LOCAL)]}
        failures = []
        async def gateway(connection, model, instructions, payload, **kwargs):
            if instructions == task_runner.WORKER and model == "shared":
                failures.append(model)
                error = provider_transport._response_error(404, "POST /chat/completions", {
                    "error": {"message": "This is not a chat model and thus not supported in the v1/chat/completions endpoint. Did you mean to use v1/completions?"},
                    "usage": {"prompt_tokens": 3, "completion_tokens": 0}})
                raise error
            return await self.infer(connection, model, instructions, payload, **kwargs)
        self.inference.side_effect = gateway
        for index in range(2):
            run = await self.submit()
            self.assertEqual(run["status"], "completed", run)
            self.assertEqual(run["tasks"][0]["model"], "my-custom:8b")
            self.assertEqual(run["tasks"][0]["routing_history"][0]["model"], "shared")
            self.assertEqual(run["fallback_count"], 1)
            self.assertEqual(run["usage"]["model_calls"], 4 if index == 0 else 3)
            self.assertEqual(run["usage"]["input_tokens"], 15 if index == 0 else 12)
        self.assertEqual(failures, ["shared"], "The next run skips the cached incompatible alias")
        self.assertEqual(set(self.settings.resolved), {LOCAL}, "Local-only never resolves remote keys")

    async def test_lead_recovery_is_used_for_review_as_well(self):
        attempted = []
        async def gateway(connection, model, instructions, payload, **kwargs):
            attempted.append((model, instructions))
            if model == "strange-draft:70b":
                raise providers.ProviderError(502, "Model unavailable", kind="model_unavailable")
            return await self.infer(connection, model, instructions, payload, **kwargs)
        self.inference.side_effect = gateway
        run = await self.submit(provider=LOCAL)
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(run["fallback_count"], 1)
        self.assertNotEqual(run["lead_model"], "strange-draft:70b")
        self.assertEqual([model for model, stage in attempted if stage == task_runner.REVIEWER], [run["lead_model"]])

    async def test_recovery_is_bounded_and_does_not_retry_same_model(self):
        self.catalogs[LOCAL] = discovered("model:1b", "model:3b", "model:7b", "model:32b", "model:70b")
        attempted = []
        async def gateway(connection, model, instructions, payload, **kwargs):
            attempted.append(model)
            raise providers.ProviderError(502, "Overloaded", kind="overloaded")
        self.inference.side_effect = gateway
        run = await self.submit()
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(len(attempted), 3)
        self.assertEqual(len(set(attempted)), 3)
        self.assertEqual(run["usage"]["model_calls"], 3)
        self.assertIn("No further approved model alternatives", run["error"])

    async def test_pinned_models_never_switch_on_recoverable_failure(self):
        for role, stage in (("lead_model", task_runner.PLANNER), ("worker_model", task_runner.WORKER)):
            self.runner.unavailable.clear()
            attempted = []
            async def gateway(connection, model, instructions, payload, **kwargs):
                if instructions == stage:
                    attempted.append(model)
                    raise providers.ProviderError(502, "Model unavailable", kind="model_unavailable")
                return await self.infer(connection, model, instructions, payload, **kwargs)
            self.inference.side_effect = gateway
            run = await self.submit(provider=LOCAL, **{role: "shared"})
            self.assertEqual(run["status"], "failed", run)
            self.assertEqual(attempted, ["shared"])
            self.assertEqual(run.get("fallback_count", 0), 0)

    async def test_recovery_cannot_bypass_revocation_or_configuration_change(self):
        for change in (lambda: self.settings.pref.update(providers=[REMOTE]),
                       lambda: self.settings.connections[LOCAL].update(context_tokens=16384)):
            self.runner.unavailable.clear()
            self.settings.pref["providers"] = [LOCAL, REMOTE]
            self.settings.connections[LOCAL]["context_tokens"] = 8192
            attempted = []
            async def gateway(connection, model, instructions, payload, **kwargs):
                attempted.append(model)
                change()
                raise providers.ProviderError(502, "Model unavailable", kind="model_unavailable")
            self.inference.side_effect = gateway
            run = await self.submit()
            self.assertEqual(run["status"], "failed", run)
            self.assertEqual(len(attempted), 1)
            self.assertEqual(run.get("fallback_count", 0), 0)

    async def test_recovery_stays_with_approved_model_ids(self):
        self.settings.pref = {"mode": "balanced", "providers": [REMOTE]}
        self.allow_models(REMOTE, ["shared"])
        self.inference.side_effect = providers.ProviderError(502, "Model unavailable", kind="model_unavailable")
        run = await self.submit()
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(run["usage"]["model_calls"], 1)
        self.assertEqual(run.get("fallback_count", 0), 0)

    async def test_context_failures_do_not_poison_availability_cache(self):
        self.plan = {"tasks": [task(model="shared", provider=LOCAL)]}
        async def gateway(connection, model, instructions, payload, **kwargs):
            if instructions == task_runner.WORKER and model == "shared":
                raise providers.ProviderError(502, "Context exceeded", kind="context_length")
            return await self.infer(connection, model, instructions, payload, **kwargs)
        self.inference.side_effect = gateway
        run = await self.submit()
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(self.runner.unavailable, {})

    async def test_auth_quota_unknown_errors_do_not_try_other_models(self):
        for status, code in ((401, "overloaded_error"), (403, "model_not_found"),
                             (402, "overloaded_error"), (429, "insufficient_quota"), (404, "unknown"), (429, "unknown")):
            self.runner.unavailable.clear()
            self.inference.reset_mock()
            self.inference.side_effect = provider_transport._response_error(status, "POST /chat/completions", {"error": {"code": code}})
            run = await self.submit()
            self.assertEqual(run["status"], "failed", run)
            self.assertEqual(self.inference.await_count, 1)
            self.assertEqual(run.get("fallback_count", 0), 0)

    async def test_recovery_can_use_an_approved_remote_alternative(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE]}
        self.catalogs[LOCAL] = discovered("local:8b")
        self.catalogs[REMOTE] = discovered("remote:70b")
        async def gateway(connection, model, instructions, payload, **kwargs):
            if connection["id"] == LOCAL and instructions == task_runner.WORKER:
                raise providers.ProviderError(502, "Unsupported model", kind="unsupported_model")
            return await self.infer(connection, model, instructions, payload, **kwargs)
        self.inference.side_effect = gateway
        run = await self.submit()
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(run["tasks"][0]["provider"], REMOTE)
        self.assertEqual(run["tasks"][0]["model"], "remote:70b")
        self.assertEqual(run["lead_provider"], REMOTE, "Review avoids the known incompatible model too")
        self.assertEqual(set(self.settings.resolved), {LOCAL, REMOTE})
        self.assertNotIn(SECRET, self.runner.store.path.read_text())

    async def test_model_recovery_cannot_exceed_run_call_cap(self):
        self.inference.side_effect = providers.ProviderError(502, "Overloaded", kind="overloaded")
        with patch.object(task_runner, "CALL_LIMIT", 1):
            run = await self.submit()
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(run["usage"]["model_calls"], 1)
        self.assertEqual(self.inference.await_count, 1)

    async def test_health_cache_expires_and_is_scoped_to_configuration(self):
        selected, _ = routing.snapshot(LOCAL)
        with patch.object(task_runner.time, "monotonic", return_value=10):
            key = self.runner._health_key(selected, LOCAL, "shared")
            self.runner.unavailable[key] = 15
            self.assertTrue(self.runner._unavailable(selected, LOCAL, "shared"))
            self.settings.connections[LOCAL]["context_tokens"] += 1
            changed, _ = routing.snapshot(LOCAL)
            self.assertFalse(self.runner._unavailable(changed, LOCAL, "shared"))
        with patch.object(task_runner.time, "monotonic", return_value=16):
            self.assertFalse(self.runner._unavailable(selected, LOCAL, "shared"))
        self.assertNotIn(key, self.runner.unavailable)

    def allow_models(self, provider, allowed):
        self.settings.connections[provider]["allowed_models"] = list(allowed)
        self.settings.profiles[provider]["allowed_models"] = list(allowed)

    async def test_custom_allowed_models_filter_explicit_and_auto_options_and_calls(self):
        self.settings.pref = {"mode": "quality", "providers": [REMOTE]}
        self.catalogs[REMOTE] = discovered("example/free-model", "example/paid:120b", "other/free-model")
        self.settings.profiles[REMOTE]["default_model"] = "example/paid:120b"
        self.allow_models(REMOTE, ["example/free-model", "not-discovered"])
        selected, _ = routing.snapshot(REMOTE)
        self.assertEqual(selected["profiles"][REMOTE]["allowed_models"], ["example/free-model", "not-discovered"])
        # The fake transport deliberately returns every model, exercising the
        # task backend's independent filter rather than hiding it in discovery.
        for provider in (REMOTE, "auto"):
            options = await self.options(provider)
            self.assertEqual([item["id"] for item in options["models"]], ["example/free-model"])
            self.assertEqual((options["lead_model"], options["worker_model"]), ("example/free-model", "example/free-model"))
            if provider == "auto":
                self.assertEqual({item["model"] for item in options["candidates"]}, {"example/free-model"})
                self.assertEqual({item["model"] for item in options["worker_candidates"]}, {"example/free-model"})
            run = await self.submit(provider=provider)
            self.assertEqual(run["status"], "completed", run)
            self.assertEqual(run["tasks"][0]["model"], "example/free-model")
        self.assertEqual({request[1] for request in self.requests}, {"example/free-model"})
        self.allow_models(REMOTE, [])
        self.assertEqual({item["id"] for item in (await self.options(REMOTE))["models"]},
                         {"example/free-model", "example/paid:120b", "other/free-model"})

    async def test_unapproved_explicit_overrides_and_auto_planner_assignment_are_blocked(self):
        self.settings.pref = {"mode": "balanced", "providers": [REMOTE]}
        self.catalogs[REMOTE] = discovered("free-model", "paid-model")
        self.allow_models(REMOTE, ["free-model"])
        for role in ("lead_model", "worker_model"):
            run = await self.submit(provider=REMOTE, **{role: "paid-model"})
            self.assertEqual(run["status"], "failed", run)
            self.assertEqual(run["usage"]["model_calls"], 0)
        self.inference.assert_not_awaited()
        self.plan = {"tasks": [task(provider=REMOTE, model="paid-model")]}
        run = await self.submit()
        self.assertEqual(run["status"], "failed", run)
        self.assertIn("invalid plan", run["error"])
        self.assertEqual(run["usage"]["model_calls"], 1)
        self.assertEqual([request[1] for request in self.requests], ["free-model"])

    async def test_allowed_models_change_stops_explicit_and_auto_runs_without_rerouting(self):
        self.settings.pref = {"mode": "balanced", "providers": [REMOTE]}
        self.catalogs[REMOTE] = discovered("free-model", "paid-model")
        for provider in (REMOTE, "auto"):
            for allowed in ([], ["paid-model"], ["free-model", "paid-model"]):
                with self.subTest(provider=provider, allowed=allowed):
                    self.allow_models(REMOTE, ["free-model"])
                    self.after_plan = lambda allowed=allowed: self.allow_models(REMOTE, allowed)
                    before = len(self.requests)
                    run = await self.submit(provider=provider)
                    self.assertEqual(run["status"], "failed", run)
                    self.assertIn("configuration changed", run["error"])
                    self.assertEqual(run["usage"]["model_calls"], 1)
                    self.assertEqual(len(self.requests) - before, 1)

    async def test_allowed_models_change_during_adapter_preparation_blocks_post(self):
        self.settings.pref = {"mode": "quality", "providers": [REMOTE]}
        self.catalogs[REMOTE] = discovered("free-model", "paid-model")
        self.allow_models(REMOTE, ["free-model"])
        self.inference.side_effect = CUSTOM_INFER
        original = provider_transport._messages
        def changed(*args):
            prepared = original(*args)
            self.allow_models(REMOTE, [])
            return prepared
        sent = []
        def respond(request):
            sent.append(request)
            return httpx.Response(500, json={})
        with patch.object(provider_transport, "_messages", side_effect=changed), \
                patch.object(provider_transport, "_encoding", return_value=models.tokenization.get_encoding("cl100k_base")), \
                patch.object(httpx, "AsyncClient", side_effect=lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(respond), **kw)):
            run = await self.submit()
        self.assertEqual(run["status"], "failed", run)
        self.assertIn("configuration changed", run["error"])
        self.assertEqual(sent, [])

    async def test_local_only_never_resolves_or_discovers_cloud(self):
        options = await self.options()
        self.assertEqual(options["mode"], "local_only")
        self.assertEqual({item["provider"] for item in options["candidates"]}, {LOCAL})
        run = await self.submit()
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(set(self.settings.resolved), {LOCAL})
        self.assertEqual({call.args[0]["id"] for call in self.discovery.await_args_list}, {LOCAL})
        self.fixed_discovery.assert_not_awaited()
        self.fixed_inference.assert_not_awaited()

    async def test_custom_explicit_local_models_and_actual_routes(self):
        run = await self.submit(provider=LOCAL, lead_model="strange-draft:70b", worker_model="shared")
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual((run["provider"], run["lead_provider"], run["worker_provider"]), (LOCAL, LOCAL, LOCAL))
        self.assertEqual(run["routing_mode"], "explicit")
        self.assertEqual(run["tasks"][0]["provider"], LOCAL)
        self.assertEqual(run["tasks"][0]["model"], "shared")
        self.assertEqual([r[1] for r in self.requests], ["strange-draft:70b", "shared", "strange-draft:70b"])
        self.assertEqual(run["usage"], {"model_calls": 3, "input_tokens": 12, "output_tokens": 6, "unknown_calls": 0})
        self.assertTrue(run["final_output"].endswith(models.LIMITATION))

    async def test_auto_local_lead_chooses_remote_worker_from_qualified_candidates(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE]}
        self.plan = {"tasks": [task(provider=REMOTE, model="remote-pro:72b"), task("b", provider=LOCAL, model="shared")]}
        options = await self.options()
        self.assertEqual(options["lead_provider"], LOCAL)
        self.assertEqual({item["id"] for item in options["models"]}, {"strange-draft:70b", "my-custom:8b", "shared"})
        self.assertIn("remote", options["reason"])
        run = await self.submit()
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(run["provider"], "auto")
        self.assertEqual(run["lead_provider"], LOCAL)
        self.assertEqual([r[0] for r in self.requests if r[2] != task_runner.WORKER], [LOCAL, LOCAL])
        self.assertEqual({r[0] for r in self.requests if r[2] == task_runner.WORKER}, {LOCAL, REMOTE})
        self.assertEqual(run["tasks"][0]["provider"], REMOTE)
        offered = self.requests[0][3]["worker_candidates"]
        self.assertTrue(all("description" in item for item in offered))
        self.assertLessEqual(len(offered), 12)

    async def test_forged_pairs_and_explicit_overrides_stop_after_planning(self):
        for provider, assignment in (
            ("auto", {"provider": REMOTE, "model": "shared"}),
            ("auto", {"provider": LOCAL, "model": "invented:999b"}),
            ("auto", {"provider": OTHER, "model": "shared"}),
            (LOCAL, {"provider": REMOTE, "model": "shared"}),
            (LOCAL, {"provider": LOCAL, "model": "strange-draft:70b"}),
        ):
            self.plan = {"tasks": [task(**assignment)]}
            before = len(self.requests)
            run = await self.submit(provider=provider, **({"worker_model": "shared"} if provider == LOCAL else {}))
            self.assertEqual(run["status"], "failed", run)
            self.assertIn("invalid plan", run["error"])
            self.assertEqual(len(self.requests) - before, 1)

    async def test_empty_allowlist_and_unknown_profiles_rejected_before_persistence(self):
        self.settings.pref["providers"] = []
        for body, status in (({"provider": "auto"}, 422), ({"provider": OTHER}, 404),
                             ({"provider": "bogus"}, 422), ({"provider": LOCAL, "api_key": SECRET}, 422)):
            response = await self.client.post(self.url, json={"goal": "Draft " + SECRET, **body})
            self.assertEqual(response.status_code, status, response.text)
            self.assertNotIn(SECRET, response.text)
        self.assertEqual(self.runner.store.list(STORE), [])
        self.assertEqual(self.settings.resolved, [])
        self.discovery.assert_not_awaited()
        self.inference.assert_not_awaited()

    async def test_auto_unqualified_overrides_are_rejected(self):
        response = await self.client.post(self.url, json={"goal": "Draft", "provider": "auto", "worker_model": "shared"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.runner.store.list(STORE), [])
        self.discovery.assert_not_awaited()

    async def test_revocation_local_only_switch_disabled_deleted_or_endpoint_changed(self):
        mutations = [lambda: self.settings.pref.update(providers=[LOCAL]),
                     lambda: self.settings.pref.update(mode="local_only"),
                     lambda: self.settings.connections[REMOTE].update(api_key=None),
                     lambda: self.settings.profiles.pop(REMOTE),
                     lambda: self.settings.connections[REMOTE].update(base_url="https://changed.invalid/v1")]
        for mutate in mutations:
            fresh = FakeSettings()
            self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE]}
            self.settings.profiles = fresh.profiles
            self.settings.connections = fresh.connections
            self.plan = {"tasks": [task(provider=REMOTE, model="shared")]}
            self.after_plan = mutate
            before = len(self.requests)
            run = await self.submit()
            self.assertEqual(run["status"], "failed", run)
            self.assertEqual(len(self.requests) - before, 1)
            self.assertEqual(run["usage"]["model_calls"], 1)
            self.assertIn("stopped", run["error"])

    async def test_allowlist_expansion_cannot_expand_submission_snapshot(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL]}
        self.plan = {"tasks": [task(provider=REMOTE, model="shared")]}
        await self.runner.calls.acquire()
        await self.runner.calls.acquire()
        try:
            response = await self.client.post(self.url, json={"provider": "auto", "goal": "Draft"})
            self.assertEqual(response.status_code, 202)
            self.settings.pref["providers"].append(REMOTE)
        finally:
            self.runner.calls.release()
            self.runner.calls.release()
        run = response.json()
        await asyncio.wait_for(self.runner.jobs[(STORE, run["id"])], 5)
        result = self.runner.store.get(STORE, run["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(set(self.settings.resolved), {LOCAL})
        self.assertEqual(len(self.requests), 1)

    async def test_custom_names_defaults_size_and_modality_filter(self):
        self.catalogs[LOCAL] = discovered("unfamiliar", "org/random:2.5b", "other:80b", "nomic-embed-text", "whisper-1", "image-only")
        options = await self.options(LOCAL)
        self.assertEqual({m["id"] for m in options["models"]}, {"unfamiliar", "org/random:2.5b", "other:80b"})
        self.assertEqual(options["lead_model"], "other:80b")
        self.settings.profiles[LOCAL]["default_model"] = "unfamiliar"
        options = await self.options(LOCAL)
        self.assertEqual(options["lead_model"], "unfamiliar")
        self.assertIn("HEURISTIC", options["reason"])
        self.settings.profiles[LOCAL]["default_model"] = "not-discovered"
        self.assertEqual((await self.options(LOCAL))["lead_model"], "other:80b")

    async def test_same_model_id_on_different_providers_remains_qualified(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE]}
        options = await self.options()
        shared = [item for item in options["candidates"] if item["model"] == "shared"]
        self.assertEqual({item["provider"] for item in shared}, {LOCAL, REMOTE})
        self.plan = {"tasks": [task(provider=REMOTE, model="shared")]}
        run = await self.submit()
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(self.requests[1][:2], (REMOTE, "shared"))

    async def test_unavailable_discovery_warning_but_generation_never_fails_over(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE]}
        async def unavailable(connection):
            if connection["id"] == REMOTE:
                raise providers.ProviderError(503, SECRET)
            return await self.discover(connection)
        self.discovery.side_effect = unavailable
        options = await self.options()
        self.assertEqual(len(options["warnings"]), 1)
        self.assertNotIn(SECRET, json.dumps(options))
        self.assertEqual({c["provider"] for c in options["candidates"]}, {LOCAL})
        run = await self.submit()
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(len(run["warnings"]), 1)
        self.discovery.side_effect = self.discover
        self.inference.reset_mock()
        self.inference.side_effect = models.InferenceError("Failure " + SECRET, {"input_tokens": 7})
        run = await self.submit()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.inference.await_count, 1)
        self.assertEqual(run["usage"]["input_tokens"], 7)
        self.assertNotIn(SECRET, json.dumps(run))

    async def test_all_route_secrets_redacted_from_goal_json_output_and_discovery(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE]}
        self.settings.connections[LOCAL]["api_key"] = LOCAL_SECRET
        self.catalogs[LOCAL]["models"].append({"id": SECRET, "name": SECRET})
        self.catalogs[LOCAL]["models"][0]["name"] = LOCAL_SECRET
        self.plan["tasks"][0]["instructions"] = SECRET + " " + LOCAL_SECRET
        options = await self.options()
        self.assertNotIn(SECRET, json.dumps(options))
        self.assertNotIn(LOCAL_SECRET, json.dumps(options))
        self.assertNotIn("[REDACTED]", [m["id"] for m in options["models"]])
        run = await self.submit(goal=SECRET + " " + LOCAL_SECRET)
        self.assertEqual(run["status"], "completed", run)
        for value in (json.dumps(run), json.dumps(self.requests), self.runner.store.path.read_text()):
            self.assertNotIn(SECRET, value)
            self.assertNotIn(LOCAL_SECRET, value)
        self.assertNotIn("api_key", self.runner.store.path.read_text())
        self.assertNotIn("base_url", self.runner.store.path.read_text())
        self.assertFalse(self.runner.secrets)
        self.assertFalse(self.runner.routes)

    async def test_remote_worker_failure_never_retries_on_local_or_other_remote(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE, "openai"]}
        self.plan = {"tasks": [task(provider=REMOTE, model="shared")]}
        async def failed_worker(connection, model, instructions, payload, *, before_send=None):
            if before_send is not None:
                connection = await before_send()
            if instructions == task_runner.WORKER:
                self.assertEqual(connection["id"], REMOTE)
                raise models.InferenceError("Remote worker failed.", {"input_tokens": 7})
            return await self.infer(connection, model, instructions, payload)
        self.inference.side_effect = failed_worker
        run = await self.submit()
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(run["tasks"][0]["provider"], REMOTE)
        self.assertEqual(run["usage"]["input_tokens"], 11)
        self.assertEqual(self.inference.await_count, 2)
        self.fixed_inference.assert_not_awaited()

    async def test_balanced_defaults_quality_and_fixed_legacy_gateway(self):
        self.settings.pref = {"mode": "balanced", "providers": [LOCAL, "openai"]}
        self.settings.profiles[LOCAL]["default_model"] = "my-custom:8b"
        options = await self.options()
        self.assertEqual((options["lead_provider"], options["lead_model"]), (LOCAL, "my-custom:8b"))
        self.settings.pref["providers"] = ["openai"]
        self.settings.profiles["openai"]["default_model"] = "gpt-5-mini"
        self.assertEqual((await self.options())["lead_model"], "gpt-5-mini")
        self.settings.pref["providers"] = [LOCAL, "openai"]
        self.settings.pref["mode"] = "quality"
        self.assertEqual((await self.options())["lead_provider"], "openai")
        run = await self.submit()
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(run["lead_provider"], "openai")
        self.assertEqual(self.fixed_inference.await_count, 3)
        self.inference.assert_not_awaited()

    async def test_quality_can_choose_larger_custom_model_than_stored_default(self):
        self.settings.pref = {"mode": "quality", "providers": [LOCAL]}
        self.settings.profiles[LOCAL]["default_model"] = "my-custom:8b"
        options = await self.options()
        self.assertEqual(options["lead_model"], "strange-draft:70b")

    async def test_endpoint_revocation_during_input_preparation_stops_network(self):
        original = models.request_input
        def changed(instructions, payload):
            text = original(instructions, payload)
            self.settings.connections[LOCAL]["base_url"] = "https://changed.invalid"
            return text
        with patch.object(models, "request_input", side_effect=changed):
            run = await self.submit()
        self.assertEqual(run["status"], "failed", run)
        # Entering the adapter is preparation, not a network send. Its hook fences I/O.
        self.assertEqual(self.requests, [])

    async def test_rotated_credentials_redacted_before_failing_call(self):
        new_secret = "rotated-private-test-credential"
        original = models.request_input
        def rotated(instructions, payload):
            text = original(instructions, payload)
            self.settings.connections[LOCAL]["api_key"] = new_secret
            return text
        sent = []
        async def failed(connection, model, instructions, payload, *, before_send=None):
            connection = await before_send()
            sent.append(connection)
            raise providers.ProviderError(403, "Denied " + new_secret)
        self.inference.side_effect = failed
        with patch.object(models, "request_input", side_effect=rotated):
            run = await self.submit(goal="Draft " + new_secret)
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(sent[0]["api_key"], new_secret)
        self.assertNotIn(new_secret, json.dumps(self.inference.await_args.args[-1]))
        self.assertNotIn(new_secret, self.runner.store.path.read_text())

    async def test_discovery_pool_and_prompt_bounds_and_global_semaphore(self):
        base = deepcopy(self.settings.connections[LOCAL])
        profile = deepcopy(self.settings.profiles[LOCAL])
        ids = ["profile_" + f"{index:032x}" for index in range(20)]
        for identifier in ids:
            self.settings.connections[identifier] = {**base, "id": identifier}
            self.settings.profiles[identifier] = {**profile, "id": identifier}
            self.catalogs[identifier] = discovered(*(f"arbitrary-{n}:8b" for n in range(550)))
        self.settings.pref = {"mode": "local_only", "providers": ids}
        active = peak = 0
        async def delayed(connection):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(.001)
                return await self.discover(connection)
            finally:
                active -= 1
        self.discovery.side_effect = delayed
        options = await self.options()
        self.assertEqual(len(options["models"]), 500)
        self.assertLessEqual(len(options["candidates"]), 120)
        self.assertLessEqual(len(options["worker_candidates"]), 12)
        self.assertEqual(peak, 2)
        self.settings.pref["providers"].append(LOCAL)
        response = await self.client.post(self.url, json={"provider": "auto", "goal": "Draft"})
        self.assertEqual(response.status_code, 422)

    async def test_custom_input_budget_and_auto_deadline(self):
        with patch.object(models, "request_input", side_effect=models.InferenceError("Input safety budget exceeded.")):
            run = await self.submit()
        self.assertEqual(run["status"], "failed")
        self.inference.assert_not_awaited()
        async def blocked(connection):
            await asyncio.Event().wait()
        self.discovery.side_effect = blocked
        with patch.object(task_runner, "RUN_TIMEOUT", .03):
            run = await self.submit()
        self.assertEqual(run["status"], "failed", run)
        self.assertIn("total timeout", run["error"])
        self.assertEqual(self.runner.calls._value, 2)

    async def test_auto_cancellation_stops_discovery_children(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE]}
        both = asyncio.Event()
        active, stopped = set(), set()
        async def blocked(connection):
            identifier = connection["id"]
            active.add(identifier)
            if len(active) == 2:
                both.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(identifier)
        self.discovery.side_effect = blocked
        response = await self.client.post(self.url, json={"provider": "auto", "goal": "Draft"})
        self.assertEqual(response.status_code, 202)
        run = response.json()
        await asyncio.wait_for(both.wait(), 3)
        response = await self.client.post(self.url + "/" + run["id"] + "/cancel", json={})
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(stopped, {LOCAL, REMOTE})
        self.inference.assert_not_awaited()
        self.assertFalse(self.runner.routes)

    async def test_catalog_only_resolves_eligible_connections_with_real_settings(self):
        settings = providers.ProviderSettings(self.runner.store.path.parent / "providers.json")
        # A real catalog call must not touch either credential resolver/keyring.
        connection = deepcopy(self.settings.connections[LOCAL])
        row = deepcopy(self.settings.profiles[LOCAL])
        with patch.object(settings, "_load", return_value={
            "models": {}, "profiles": {LOCAL: {**row, "model": "shared", "credential_storage": "none"}},
            "routing": {"mode": "local_only", "providers": [LOCAL, "openai", "anthropic"]},
        }), patch.object(settings, "_resolve", side_effect=AssertionError("Remote credential resolved")), \
                patch.object(settings, "_profile_resolve", side_effect=AssertionError("Catalog resolved profile key")), \
                patch.object(settings, "_backend", side_effect=AssertionError("Keyring accessed")), \
                patch.object(settings, "connection", side_effect=lambda identifier: deepcopy(connection) if identifier == LOCAL else self.fail("Cloud connection resolved")) as resolve, \
                patch.object(provider_api, "settings", settings):
            options = await self.options()
        self.assertEqual(options["lead_provider"], LOCAL)
        self.assertTrue(resolve.call_count)
        self.assertTrue(all(call.args == (LOCAL,) for call in resolve.call_args_list))

    async def test_cloud_models_on_local_endpoint_are_filtered_and_labelled(self):
        self.discovery.side_effect = CUSTOM_DISCOVER
        self.settings.pref["providers"] = [LOCAL]
        sent = []
        def respond(request):
            sent.append(request)
            self.assertEqual(request.url.path, "/api/tags")
            return httpx.Response(200, json={"models": [
                {"name": "gpt-oss:120b-cloud"},
                {"name": "innocent:70b", "remote_host": "https://example.invalid", "remote_model": "remote"},
                {"name": "local:8b"},
            ]})
        with patch.object(httpx, "AsyncClient", side_effect=lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(respond), **kw)):
            options = await self.options()
            self.assertEqual([item["id"] for item in options["models"]], ["local:8b"])
            self.assertTrue(all(item["is_local"] for item in options["candidates"]))
            self.assertTrue(self.discovery.await_args.args[0]["local_only"])
            self.plan = {"tasks": [task(provider=LOCAL, model="gpt-oss:120b-cloud")]}
            run = await self.submit()
            self.assertEqual(run["status"], "failed", run)
            self.assertEqual(run["usage"]["model_calls"], 1)
            self.settings.pref["mode"] = "local_first"
            options = await self.options()
        self.assertEqual(options["lead_model"], "local:8b")
        locality = {item["model"]: item["is_local"] for item in options["candidates"]}
        self.assertEqual(locality, {"local:8b": True, "innocent:70b": False, "gpt-oss:120b-cloud": False})
        self.assertTrue(all(request.method == "GET" for request in sent))

    async def test_local_only_rejects_nonlocal_discovery_metadata_even_if_adapter_returns_it(self):
        self.catalogs[LOCAL] = {"models": [
            {"id": "cloud-alias", "name": "Cloud alias", "is_local": False},
            {"id": "local:8b", "name": "Local", "is_local": True},
        ]}
        options = await self.options()
        self.assertEqual([item["model"] for item in options["candidates"]], ["local:8b"])
        self.catalogs[LOCAL]["models"].pop()
        run = await self.submit()
        self.assertEqual(run["status"], "failed", run)
        self.inference.assert_not_awaited()

    async def test_current_local_only_policy_filters_models_after_discovery_wait(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL]}
        self.catalogs[LOCAL] = {"models": [
            {"id": "cloud:120b", "name": "Cloud", "is_local": False},
            {"id": "local:8b", "name": "Local", "is_local": True},
        ]}
        async def tightened(connection):
            self.assertFalse(connection["local_only"])
            result = await self.discover(connection)
            self.settings.pref["mode"] = "local_only"
            return result
        self.discovery.side_effect = tightened
        options = await self.options()
        self.assertEqual([item["model"] for item in options["candidates"]], ["local:8b"])

    async def test_unconfigured_eligible_connection_is_excluded_after_resolution(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE]}
        self.settings.connections[REMOTE]["api_key"] = None
        options = await self.options()
        self.assertIn(REMOTE, self.settings.resolved)
        self.assertEqual({item["provider"] for item in options["candidates"]}, {LOCAL})
        self.assertTrue(options["warnings"])
        self.assertNotIn(REMOTE, [call.args[0]["id"] for call in self.discovery.await_args_list])
        response = await self.client.post(self.url, json={"provider": REMOTE, "goal": "Draft"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.runner.store.list(STORE), [])

    async def test_real_adapters_revalidate_after_preparation_before_remote_post(self):
        for provider in (REMOTE, "openai", LOCAL):
            for action in ("remove", "local_only", "cancel", "delete_store"):
                with self.subTest(provider=provider, action=action):
                    self.settings.pref = {"mode": "local_first", "providers": [provider]}
                    if provider == LOCAL:
                        # A cloud model on a local endpoint must obey a newly tightened policy too.
                        self.catalogs[LOCAL] = {"models": [{"id": "gpt-oss:120b-cloud", "name": "Cloud", "is_local": False}]}
                    self.inference.side_effect = CUSTOM_INFER
                    self.fixed_inference.side_effect = FIXED_INFER
                    entered, release = asyncio.Event(), threading.Event()
                    loop = asyncio.get_running_loop()
                    module = models if provider == "openai" else provider_transport
                    attribute = "request_input" if provider == "openai" else "_messages"
                    original = getattr(module, attribute)
                    def blocked(*args):
                        prepared = original(*args)
                        loop.call_soon_threadsafe(entered.set)
                        if not release.wait(5):
                            raise AssertionError("Preparation barrier timed out")
                        return prepared
                    sent = []
                    def respond(request):
                        sent.append(request)
                        return httpx.Response(500, json={})
                    with patch.object(module, attribute, side_effect=blocked), \
                            patch.object(provider_transport, "_encoding", return_value=models.tokenization.get_encoding("cl100k_base")), \
                            patch.object(httpx, "AsyncClient", side_effect=lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(respond), **kw)):
                        response = await self.client.post(self.url, json={"provider": "auto", "goal": "Never send this goal after revocation"})
                        self.assertEqual(response.status_code, 202, response.text)
                        run = response.json()
                        job = self.runner.jobs[(STORE, run["id"])]
                        try:
                            await asyncio.wait_for(entered.wait(), 3)
                            if action == "remove":
                                self.settings.pref["providers"] = []
                            elif action == "local_only":
                                self.settings.pref["mode"] = "local_only"
                            elif action == "delete_store":
                                storage.get_store.return_value = None
                            else:
                                cancel = asyncio.create_task(self.runner.cancel(STORE, run["id"]))
                                while (STORE, run["id"]) not in self.runner.stopped:
                                    await asyncio.sleep(0)
                        finally:
                            release.set()
                        if action == "cancel":
                            await asyncio.wait_for(cancel, 3)
                        else:
                            await asyncio.wait_for(job, 3)
                        storage.get_store.return_value = {"id": STORE}
                    result = self.runner.store.get(STORE, run["id"])
                    self.assertEqual(result["status"], "cancelled" if action == "cancel" else "failed", result)
                    self.assertEqual(sent, [], "No request may cross the adapter after preparation revoked authorization")

    async def test_local_show_rechecks_authorization_and_cloud_backing_before_chat(self):
        self.inference.side_effect = CUSTOM_INFER
        self.settings.pref = {"mode": "local_only", "providers": [LOCAL]}
        self.catalogs[LOCAL] = {"models": [{"id": "local-alias", "name": "Local alias", "is_local": True}]}
        for action in ("remove", "cloud_backing"):
            self.settings.pref["providers"] = [LOCAL]
            sent = []
            def respond(request):
                sent.append(request)
                if request.url.path == "/api/show":
                    if action == "remove":
                        self.settings.pref["providers"] = []
                        return httpx.Response(200, json={})
                    return httpx.Response(200, json={"remote_host": "https://example.invalid", "remote_model": "cloud"})
                return httpx.Response(500, json={})
            with patch.object(provider_transport, "_encoding", return_value=models.tokenization.get_encoding("cl100k_base")), \
                    patch.object(httpx, "AsyncClient", side_effect=lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(respond), **kw)):
                run = await self.submit()
            self.assertEqual(run["status"], "failed", run)
            self.assertEqual([request.url.path for request in sent], ["/api/show"])
            self.assertNotIn("goal", sent[0].content.decode())

    async def test_fixed_adapter_uses_rotated_key_and_scrubs_prepared_payload(self):
        self.settings.pref = {"mode": "quality", "providers": ["openai"]}
        self.fixed_inference.side_effect = FIXED_INFER
        fresh = "rotated-fixed-provider-credential"
        original = models.request_input
        def rotated(instructions, payload):
            prepared = original(instructions, payload)
            self.settings.connections["openai"]["api_key"] = fresh
            return prepared
        sent = []
        def respond(request):
            sent.append(request)
            return httpx.Response(403, json={"error": "Denied"})
        with patch.object(models, "request_input", side_effect=rotated), \
                patch.object(httpx, "AsyncClient", side_effect=lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(respond), **kw)):
            run = await self.submit(goal="Draft " + fresh)
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].headers["authorization"], "Bearer " + fresh)
        self.assertNotIn(fresh, sent[0].content.decode())
        self.assertNotIn(fresh, self.runner.store.path.read_text())

    async def test_custom_prepared_payload_cannot_forward_another_routes_new_secret(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE]}
        self.inference.side_effect = CUSTOM_INFER
        fresh = "newly-used-other-route-credential"
        original = provider_transport._messages
        def prepared(connection, model, instructions, payload):
            messages = original(connection, model, instructions, payload)
            # A parallel worker may refresh another route's key while this adapter
            # prepares messages. Its key must not escape in this stale copy.
            for secrets in self.runner.secrets.values():
                secrets.add(fresh)
            return messages
        sent = []
        def respond(request):
            sent.append(request)
            return httpx.Response(500, json={})
        with patch.object(provider_transport, "_messages", side_effect=prepared), \
                patch.object(provider_transport, "_encoding", return_value=models.tokenization.get_encoding("cl100k_base")), \
                patch.object(httpx, "AsyncClient", side_effect=lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(respond), **kw)):
            run = await self.submit(goal="Draft " + fresh)
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(sent, [])


    async def test_native_planner_success_then_worker_http_failure_is_attributed(self):
        self.inference.side_effect = CUSTOM_INFER
        self.discovery.side_effect = CUSTOM_DISCOVER
        self.settings.connections[LOCAL]["api_key"] = LOCAL_SECRET
        cases = [(404, {"error": f"model '{SECRET}' not found, try pulling it first"}, "Model not found"),
                 (400, {"error": {"code": "context_length_exceeded", "message": SECRET}}, "Context length exceeded"),
                 (400, {"error": f'"{SECRET}" does not support chat'}, "does not support chat"),
                 (503, {"error": {"type": "overloaded_error", "message": LOCAL_SECRET}}, "overloaded")]
        for status, failure, category in cases:
            with self.subTest(status=status, category=category):
                sent = []
                def respond(request):
                    sent.append(request)
                    if request.url.path == "/api/tags":
                        return httpx.Response(200, json={"models": [{"name": "strange-draft:70b"}, {"name": "shared"}]})
                    self.assertEqual(request.url.path, "/api/chat")
                    body = json.loads(request.content)
                    if body["messages"][0]["content"] == task_runner.PLANNER:
                        self.assertEqual(body["model"], "strange-draft:70b")
                        return httpx.Response(200, json={"done": True, "message": {"content": json.dumps(self.plan)},
                                                        "prompt_eval_count": 4, "eval_count": 2})
                    self.assertEqual(body["messages"][0]["content"], task_runner.WORKER)
                    self.assertEqual(body["model"], "shared")
                    return httpx.Response(status, json={**failure, "prompt_eval_count": 7, "eval_count": 1})
                with patch.object(provider_transport, "_encoding", return_value=models.tokenization.get_encoding("cl100k_base")), \
                        patch.object(httpx, "AsyncClient", side_effect=lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(respond), **kw)):
                    run = await self.submit(provider=LOCAL, lead_model="strange-draft:70b", worker_model="shared")
                self.assertEqual(run["status"], "failed", run)
                self.assertEqual(run["reviews"], [])
                self.assertIsNone(run["final_output"])
                self.assertEqual([r.url.path for r in sent], ["/api/tags", "/api/chat", "/api/chat"])
                self.assertEqual(run["usage"], {"model_calls": 2, "input_tokens": 11, "output_tokens": 3, "unknown_calls": 0})
                worker = run["tasks"][0]
                self.assertEqual((worker["status"], worker["provider"], worker["model"], worker["attempts"]),
                                 ("failed", LOCAL, "shared", 1))
                self.assertEqual(worker["error"], run["error"])
                for text in (run["error"], next(e["message"] for e in run["events"] if "Task a failed" in e["message"])):
                    for expected in ("Task a failed", LOCAL, "shared", f"HTTP {status}", "POST /api/chat", category):
                        self.assertIn(expected, text)
                for secret in (SECRET, LOCAL_SECRET):
                    self.assertNotIn(secret, json.dumps(run))
                    self.assertNotIn(secret, self.runner.store.path.read_text())
        self.fixed_inference.assert_not_awaited()

    async def test_auth_failure_names_actual_remote_worker_and_stops_local_sibling(self):
        self.settings.pref = {"mode": "local_first", "providers": [LOCAL, REMOTE]}
        self.settings.connections[LOCAL]["api_key"] = LOCAL_SECRET
        self.plan = {"tasks": [task("failed-remote", provider=REMOTE, model="shared"),
                               task("local-sibling", provider=LOCAL, model="strange-draft:70b"),
                               {**task("dependent", provider=LOCAL, model="shared"), "depends_on": ["failed-remote"]}]}
        self.inference.side_effect = CUSTOM_INFER
        sibling_started, sibling_stopped = asyncio.Event(), asyncio.Event()
        sent = []
        class WaitingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                sibling_started.set()
                try:
                    await asyncio.Event().wait()
                    yield b""
                finally:
                    sibling_stopped.set()
        async def respond(request):
            body = json.loads(request.content)
            sent.append((request.url.path, body["model"], body["messages"][0]["content"]))
            if body["messages"][0]["content"] == task_runner.PLANNER:
                return httpx.Response(200, json={"done": True, "message": {"content": json.dumps(self.plan)},
                                                "prompt_eval_count": 4, "eval_count": 2})
            self.assertEqual(body["messages"][0]["content"], task_runner.WORKER)
            if request.url.path == "/api/chat":
                return httpx.Response(200, stream=WaitingBody())
            await sibling_started.wait()
            return httpx.Response(401, json={"error": {"type": "overloaded_error", "code": {"key": SECRET},
                                                      "message": SECRET + LOCAL_SECRET},
                                            "usage": {"prompt_tokens": 7, "completion_tokens": 1}})
        with patch.object(provider_transport, "_encoding", return_value=models.tokenization.get_encoding("cl100k_base")), \
                patch.object(httpx, "AsyncClient", side_effect=lambda **kw: HTTP_CLIENT(transport=httpx.MockTransport(respond), **kw)):
            run = await self.submit()
        failed, sibling, dependent = run["tasks"]
        self.assertEqual(run["status"], "failed", run)
        self.assertTrue(sibling_stopped.is_set())
        self.assertEqual(failed["error"], run["error"])
        for expected in ("failed-remote", REMOTE, "shared", "HTTP 401", "POST /chat/completions", "Authentication failed"):
            self.assertIn(expected, run["error"])
        self.assertNotIn(LOCAL, run["error"])
        self.assertEqual((sibling["status"], sibling["error"]), ("failed", "Stopped because another task failed."))
        self.assertEqual((dependent["status"], dependent["error"], dependent["attempts"]), ("pending", None, 0))
        self.assertEqual(run["reviews"], [])
        self.assertEqual(len(sent), 3)
        self.assertEqual(run["usage"], {"model_calls": 3, "input_tokens": 11, "output_tokens": 3, "unknown_calls": 1})
        self.assertEqual(self.runner.calls._value, 2)
        self.fixed_inference.assert_not_awaited()
        self.assertTrue(any(e["message"] == failed["error"] for e in run["events"]))
        for secret in (SECRET, LOCAL_SECRET):
            self.assertNotIn(secret, json.dumps(run))
            self.assertNotIn(secret, self.runner.store.path.read_text())

    async def test_worker_attribution_scrubs_all_metadata_before_bounding(self):
        for exception_type in (models.InferenceError, providers.ProviderError):
            run = self.runner.store.create(STORE, models.RunInput(provider=LOCAL, goal="Draft").model_dump())
            key = (STORE, run["id"])
            self.runner.secrets[key] = {SECRET, LOCAL_SECRET}
            identifier = "t" * 25 + SECRET
            provider = "p" * 25 + LOCAL_SECRET
            model = "m" * 90 + SECRET + "end"
            def prepare(value):
                value["status"] = "running"
                value["tasks"] = [{**task(identifier, provider=provider, model=model), "status": "running",
                                   "attempts": 2, "output": "Prior draft", "error": None}]
            self.runner.store.update(STORE, run["id"], prepare)
            cause = "HTTP 400 (POST /api/chat): Context length exceeded. " + SECRET + LOCAL_SECRET + "x" * 500
            error = providers.ProviderError(502, cause) if exception_type is providers.ProviderError else models.InferenceError(cause)
            with patch.object(self.runner, "_call", side_effect=error), self.assertRaises(models.InferenceError) as raised:
                await self.runner._worker(STORE, run["id"], identifier, model, {}, provider)
            saved = self.runner.store.get(STORE, run["id"])
            worker = saved["tasks"][0]
            self.assertEqual(worker["output"], "Prior draft")
            self.assertEqual(worker["status"], "failed")
            self.assertEqual(worker["model"], routing.scrub(model, {SECRET, LOCAL_SECRET}))
            self.assertEqual(worker["provider"], routing.scrub(provider, {SECRET, LOCAL_SECRET}))
            self.assertIn("HTTP 400 (POST /api/chat): Context length exceeded", worker["error"])
            self.assertLessEqual(len(worker["error"]), 500)
            self.assertEqual(str(raised.exception), worker["error"])
            for secret in (SECRET, LOCAL_SECRET):
                self.assertNotIn(secret, self.runner.store.path.read_text())
            self.runner.store.finish(STORE, run["id"], "failed", str(raised.exception))
            self.runner.secrets.pop(key)


if __name__ == "__main__":
    unittest.main()
