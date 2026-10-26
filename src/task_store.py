"""Separate task-run JSON, serialized across threads/processes and atomically replaced."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
from uuid import uuid4

from providers import ProviderError
from inference_policy import next_action
import task_usage


ACTIVE = frozenset({"queued", "discovering", "planning", "running", "reviewing"})
EVENT_LIMIT = 100
LIST_LIMIT = 50
ADMISSION_LIMIT = 20
_MUTEX = threading.RLock()


def now():
    return datetime.now(timezone.utc).isoformat()


def event(run, message):
    run["events"].append({"time": now(), "message": message})
    run["events"] = run["events"][-EVENT_LIMIT:]


class TaskStore:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else Path.home() / ".self_mcp/task_runs.json"
        self._owner = None

    def acquire_owner(self):
        """Hold a separate, nonblocking process lease for the entire API lifespan."""
        if self._owner is not None:
            raise ProviderError(503, "Task runner is already started.")
        owner = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path.with_suffix(".runner.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            owner = os.fdopen(fd, "a+")
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                owner.seek(0)
                pid = owner.read(32).strip()
                identity = f" (PID {pid})" if pid.isascii() and pid.isdecimal() else ""
                raise ProviderError(503, f"Another self_mcp process{identity} still holds the task-runner lock, even if port 8000 is free. Stop its terminal with Ctrl+C and wait for it to exit before starting again.") from None
            # Informational only: the OS lock, never this PID, determines ownership.
            owner.seek(0)
            owner.truncate()
            owner.write(str(os.getpid()))
            owner.flush()
            self._owner = owner
        except OSError:
            raise ProviderError(503, "Cannot access the task-runner lock. Check permissions and free space in ~/.self_mcp.") from None
        finally:
            if owner is not None and self._owner is not owner:
                owner.close()

    def release_owner(self):
        if self._owner is not None:
            self._owner.close()
            self._owner = None

    @contextmanager
    def transaction(self, write=False):
        with _MUTEX:
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
                with os.fdopen(fd, "a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        data = json.loads(self.path.read_text()) if self.path.exists() else {"runs": []}
                        if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
                            raise ValueError
                        yield data
                        if write:
                            self._write(data)
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
            except (OSError, ValueError, TypeError):
                raise ProviderError(503, "Task-run persistence is unavailable or invalid.") from None

    def _write(self, data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as output:
                temporary = output.name
                json.dump(data, output, ensure_ascii=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def fresh(store_id, body):
        stamp = now()
        run = {"id": str(uuid4()), "store_id": store_id, **body, "status": "queued",
               "lead_provider": None, "worker_provider": None, "routing_mode": body.get("routing_mode", "explicit"),
               "worker_candidates": [], "warnings": [],
               "tasks": [], "reviews": [], "events": [], "final_output": None, "error": None,
               "created_at": stamp, "updated_at": stamp, "turn_started_at": stamp, "revision_count": 0,
               "turn_id": str(uuid4()), "turns": [], "root_goal": body["goal"],
               "usage": {"input_tokens": 0, "output_tokens": 0, "model_calls": 0, "unknown_calls": 0}}
        event(run, "Queued for automatic execution; model calls may incur charges.")
        return run

    def create(self, store_id, body):
        run = self.fresh(store_id, body)
        with self.transaction(write=True) as data:
            if sum(run["status"] in ACTIVE for run in data["runs"]) >= ADMISSION_LIMIT:
                raise ProviderError(429, "Task queue is full (20 active runs). Wait or cancel a run.")
            data["runs"].append(run)
        return deepcopy(run)

    def continue_run(self, store_id, run_id, body, message_id, expected_turn_id, submission_hash):
        """Atomically archive the finished turn and admit one follow-up on the same ID."""
        with self.transaction(write=True) as data:
            run = self._find(data, store_id, run_id)
            prior = next((turn for turn in [run, *run.get("turns", [])]
                          if turn.get("turn_id", turn.get("id")) == message_id), None)
            if prior is not None:
                if prior.get("submission_hash") != submission_hash:
                    raise ProviderError(409, "This message ID was already used for different content. Refresh the task before sending.")
                return deepcopy(run), False
            if run["status"] in ACTIVE:
                raise ProviderError(409, "This task is still working. Wait for the answer or cancel it before sending a follow-up.")
            if run.get("turn_id", run["id"]) != expected_turn_id:
                raise ProviderError(409, "This task has a newer message. Refresh it before sending your follow-up.")
            if sum(item["status"] in ACTIVE for item in data["runs"]) >= ADMISSION_LIMIT:
                raise ProviderError(429, "Task queue is full (20 active runs). Wait or cancel a run.")
            archived = {key: deepcopy(value) for key, value in run.items()
                        if key not in {"turns", "root_goal", "turn_start_usage"}}
            archived["turn_id"] = run.get("turn_id", run["id"])
            archived["usage"] = {key: value - run.get("turn_start_usage", {}).get(key, 0)
                                 for key, value in run["usage"].items()}
            fresh = self.fresh(store_id, body)
            fresh.update(id=run_id, created_at=run["created_at"], root_goal=run.get("root_goal", run["goal"]),
                         turns=[*run.get("turns", []), archived], turn_id=message_id,
                         submission_hash=submission_hash, usage=deepcopy(run["usage"]),
                         turn_start_usage=deepcopy(run["usage"]))
            run.clear()
            run.update(fresh)
            return deepcopy(run), True

    def list(self, store_id, offset=0, limit=LIST_LIMIT):
        with self.transaction() as data:
            return deepcopy(sorted((run for run in data["runs"] if run["store_id"] == store_id),
                                   key=lambda run: (run["updated_at"], run["id"]), reverse=True)[offset:offset + limit])

    def usage_details(self, store_id, run_id):
        with self.transaction() as data:
            run = self._find(data, store_id, run_id)
            prices = data.get("prices", {}).get(store_id, {})
            ledger = data.get("call_usage", {})
            details = task_usage.summarize(run, ledger.get(run_id, {}), prices)
            details["project_total"] = task_usage.project_totals([
                task_usage.summarize(item, ledger.get(item["id"], {}), prices)
                for item in data["runs"] if item["store_id"] == store_id])
            return details

    def save_price(self, store_id, body):
        with self.transaction(write=True) as data:
            data.setdefault("prices", {}).setdefault(store_id, {})[task_usage.price_key(body["provider"], body["model"])] = {
                "input_per_million": body["input_per_million"], "output_per_million": body["output_per_million"],
                "updated_at": now(), "currency": "USD"}

    def get(self, store_id, run_id):
        with self.transaction() as data:
            return deepcopy(self._find(data, store_id, run_id))

    @staticmethod
    def _find(data, store_id, run_id):
        run = next((r for r in data["runs"] if r["id"] == run_id and r["store_id"] == store_id), None)
        if run is None:
            raise ProviderError(404, "Task run not found in this store.")
        return run

    def update(self, store_id, run_id, change, expected=None):
        # Terminal workflow state is immutable; only account_usage may add late counts.
        with self.transaction(write=True) as data:
            run = self._find(data, store_id, run_id)
            if run["status"] not in ACTIVE or (expected is not None and run["status"] not in expected):
                return None
            change(run)
            run["updated_at"] = now()
            return deepcopy(run)

    def finish(self, store_id, run_id, status, message):
        def change(run):
            worker_failed = status == "failed" and any(task["status"] == "failed" for task in run["tasks"])
            run["stopped_stage"] = run["status"]
            run["status"] = status
            run["error"] = message
            if status == "failed":
                run["error_action"] = next_action(message)
            for task in run["tasks"]:
                if task["status"] == "running":
                    task["status"] = "failed"
                    task["error"] = "Stopped because another task failed." if worker_failed else message
            event(run, message)
        self.update(store_id, run_id, change)
        return self.get(store_id, run_id)

    def account_usage(self, store_id, run_id, call_id, usage):
        """Only accounting may change after termination; first known count wins per call."""
        with self.transaction(write=True) as data:
            run = self._find(data, store_id, run_id)
            if type(call_id) is not int or not 1 <= call_id <= run["usage"]["model_calls"]:
                raise ProviderError(422, "Unknown model-call accounting ID.")
            # Private ledger stays outside API run objects; no text or credentials enter it.
            ledger = data.setdefault("call_usage", {}).setdefault(run_id, {})
            previous = ledger.setdefault(str(call_id), {"input_tokens": None, "output_tokens": None})
            was_known = all(value is not None for value in previous.values())
            for key in previous:
                count = usage.get(key)
                if previous[key] is None and type(count) is int and count >= 0:
                    previous[key] = count
                    run["usage"][key] += count
            if not was_known and all(value is not None for value in previous.values()):
                run["usage"]["unknown_calls"] = max(0, run["usage"].get("unknown_calls", 0) - 1)

    def interrupt_active(self):
        with self.transaction(write=True) as data:
            for run in data["runs"]:
                if run["status"] in ACTIVE:
                    run["status"] = "interrupted"
                    run["error"] = "API process stopped; no automatic replay or paid calls. Send a follow-up on this task to continue."
                    run["updated_at"] = now()
                    for task in run["tasks"]:
                        if task["status"] == "running":
                            task["status"] = "failed"
                            task["error"] = run["error"]
                    event(run, run["error"])
