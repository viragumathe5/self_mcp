"""Conservative model discovery, strict lead schemas, and text-only inference."""

import asyncio
import json
import re
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool
import tiktoken

import providers


Provider = Literal["openai", "anthropic"]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TaskID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,40}$")]
MAX_INPUT_TOKENS = 60000
MAX_INPUT_BYTES = 200000
MAX_OUTPUT_CHARS = 24000
LIMITATION = "This is model-based text/code drafting and review only. Code and simulations have not been executed; correctness has not been verified."


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelOptionsInput(StrictInput):
    provider: Provider


class RunInput(ModelOptionsInput):
    goal: Text = Field(max_length=12000)
    lead_model: str | None = Field(default=None, max_length=200, pattern=f"^{providers.MODEL_PATTERN}$")
    worker_model: str | None = Field(default=None, max_length=200, pattern=f"^{providers.MODEL_PATTERN}$")
    max_tasks: int = Field(default=6, ge=1, le=8)
    max_revisions: int = Field(default=2, ge=0, le=3)


class PlannedTask(StrictInput):
    id: TaskID
    title: Text = Field(max_length=200)
    instructions: Text = Field(max_length=6000)
    depends_on: list[TaskID] = Field(max_length=8)
    acceptance_criteria: list[Annotated[Text, Field(max_length=500)]] = Field(min_length=1, max_length=8)


class Plan(StrictInput):
    tasks: list[PlannedTask] = Field(min_length=1, max_length=8)


class Review(StrictInput):
    approved: bool
    feedback: Text = Field(max_length=6000)
    revision_tasks: list[TaskID] = Field(max_length=8)
    final_output: Annotated[Text, Field(max_length=16000)] | None


def redact(value, credential: str):
    """Redact before validation or persistence, including keys echoed in JSON fields."""
    if isinstance(value, str):
        value = value.replace(credential, "[REDACTED]") if credential else value
        return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
    if isinstance(value, list):
        return [redact(item, credential) for item in value]
    if isinstance(value, dict):
        return {redact(key, credential): redact(item, credential) for key, item in value.items()}
    return value


def parse_json(text: str, schema=None):
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    value = json.loads(text)
    return schema.model_validate(value) if schema is not None else value


def validate_plan(text: str, max_tasks: int) -> Plan:
    plan = parse_json(text, Plan)
    ids = {task.id for task in plan.tasks}
    if len(plan.tasks) > max_tasks or len(ids) != len(plan.tasks):
        raise ValueError("Invalid task count or duplicate task IDs.")
    remaining = {}
    for task in plan.tasks:
        deps = set(task.depends_on)
        if len(deps) != len(task.depends_on) or task.id in deps or not deps <= ids:
            raise ValueError("Invalid task dependencies.")
        remaining[task.id] = deps
    done = set()
    while remaining:
        ready = {task_id for task_id, deps in remaining.items() if deps <= done}
        if not ready:
            raise ValueError("Cyclic task dependencies.")
        done.update(ready)
        remaining = {task_id: deps for task_id, deps in remaining.items() if task_id not in ready}
    return plan


def validate_review(text: str, task_ids: set[str]) -> Review:
    review = parse_json(text, Review)
    revisions = set(review.revision_tasks)
    if len(revisions) != len(review.revision_tasks) or not revisions <= task_ids:
        raise ValueError("Invalid revision task IDs.")
    if review.approved:
        if revisions or not review.final_output:
            raise ValueError("Approval requires a final output and no revision tasks.")
    elif not revisions or review.final_output is not None:
        raise ValueError("Rejection requires revision tasks and a null final output.")
    return review


def model_options(provider: str, discovered: dict) -> dict:
    providers.validate_provider(provider)
    excluded = re.compile(r"embedding|audio|image|realtime|tts|whisper|transcri|moderation|search|deep-research|instruct|codex|computer|dall-e", re.I)
    recognized = r"claude-(?:(?:opus|sonnet|haiku)-\d[\d.-]*|\d[\d.-]*-(?:opus|sonnet|haiku))(?:-[a-z0-9.-]+)?"
    # GPT-4o has a letter suffix; do not accidentally include arbitrary gpt-* names.
    if provider == "openai":
        recognized = r"(?:gpt-\d+(?:\.\d+)*(?:o)?(?:-[a-z0-9.-]+)?|o[1-9]\d*(?:-[a-z0-9.-]+)?)"
        # Discovery access does not imply compatibility with the Responses endpoint.
        excluded = re.compile(excluded.pattern + r"|^(?:o1-(?:mini|preview)(?:-|$)|gpt-3(?:\.5)?(?:-|$)|gpt-4(?:-|$))", re.I)
    models = sorted({item["id"]: {"id": item["id"], "name": item["name"]}
                     for item in discovered["models"]
                     if re.fullmatch(recognized, item["id"]) and not excluded.search(item["id"])}.values(), key=lambda item: item["id"])

    def rank(model):
        name = model["id"]
        numbers = tuple(int(n) for n in re.findall(r"\d+", name) if len(n) < 6)
        if provider == "anthropic":
            tier = 3 if "opus" in name else 2 if "sonnet" in name else 1 if "haiku" in name else 0
            return tier, numbers, name
        match = re.match(r"gpt-(\d+)(?:\.(\d+))?", name)
        version = (int(match[1]), int(match[2] or 0)) if match else (4, int(name[1:].split("-")[0]))
        tier = 0 if "nano" in name else 1 if "mini" in name else 2
        return tier, version, numbers, name

    safe = [m for m in models if (
        any(family in m["id"] for family in ("opus", "sonnet", "haiku")) if provider == "anthropic"
        else bool(re.match(r"(?:gpt-(?:[5-9]|[1-9]\d)|gpt-4(?:\.[1-9]\d*|o)(?:-|$)|o(?:[3-9]|[1-9]\d)(?:-|$))", m["id"])))]
    lead = max(safe, key=rank)["id"] if safe else None
    cheap = [m for m in safe if any(part in m["id"] for part in (("mini", "nano") if provider == "openai" else ("haiku",)))]
    if provider == "anthropic" and not cheap:
        cheap = [m for m in safe if "sonnet" in m["id"]]
    worker = max(cheap or safe, key=rank)["id"] if safe else None
    reason = "HEURISTIC: family, tier and version ranking; prefer smaller workers. Known endpoint-incompatible legacy IDs are excluded. Not a best-model, price, or inference-access guarantee."
    if not safe:
        reason += " No safe automatic recommendation; explicitly select compatible models from this generation-model list."
    if "truncated" in discovered.get("message", "").lower():
        reason += " Provider discovery was truncated at its safety limit."
    return {"models": models, "lead_model": lead, "worker_model": worker, "reason": reason}


class InferenceError(Exception):
    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage if usage is not None else {}


def request_input(instructions: str, payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    combined = instructions + "\n" + text
    if len(combined.encode("utf-8")) > MAX_INPUT_BYTES or len(tiktoken.get_encoding("cl100k_base").encode(combined, disallowed_special=())) > MAX_INPUT_TOKENS:
        raise InferenceError("Model request exceeded the input safety budget; reduce the goal or task scope.")
    return text


async def infer(provider: str, credential: str, model: str, instructions: str, payload: dict) -> dict:
    providers.validate_provider(provider)
    text = await run_in_threadpool(request_input, instructions, payload)
    if provider == "openai":
        url = "https://api.openai.com/v1/responses"
        headers = {"Authorization": f"Bearer {credential}"}
        body = {"model": model, "instructions": instructions, "input": text, "max_output_tokens": 6000, "store": False}
    else:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": credential, "anthropic-version": "2023-06-01"}
        body = {"model": model, "system": instructions, "messages": [{"role": "user", "content": text}], "max_tokens": 6000}
    usage = {}
    try:
        async with asyncio.timeout(90), httpx.AsyncClient(timeout=90, follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        raise InferenceError("Provider response exceeded the safety limit.")
                status = response.status_code
                retry_after = response.headers.get("retry-after")
        if status != 200:
            # Some error responses contain billable usage. Never retain their text.
            try:
                counts = json.loads(raw).get("usage", {})
                for name in ("input_tokens", "output_tokens"):
                    count = counts.get(name)
                    if type(count) is int and count >= 0:
                        usage[name] = count
            except (ValueError, TypeError, AttributeError):
                pass
            if status == 429:
                raise InferenceError(providers.limit_error_message(provider, raw, retry_after), usage)
            messages = {401: "Provider authentication failed.", 403: "Provider denied inference access to the selected model."}
            raise InferenceError(messages.get(status, "Provider inference failed for the selected model; no fallback was attempted."), usage)
        data = json.loads(raw)
        for name in ("input_tokens", "output_tokens"):
            count = data.get("usage", {}).get(name)
            if count is None:
                continue
            if type(count) is not int or count < 0:
                raise ValueError
            usage[name] = count
        if provider == "openai":
            if data.get("status") != "completed" or data.get("error"):
                raise InferenceError("Provider returned incomplete or failed output.", usage)
            parts = [part for item in data.get("output", []) if item.get("type") == "message" for part in item.get("content", [])]
            if any(part.get("type") == "refusal" for part in parts):
                raise InferenceError("Provider refused the request.", usage)
            result = "\n".join(part["text"] for part in parts if part.get("type") == "output_text")
        else:
            if data.get("stop_reason") != "end_turn":
                raise InferenceError("Provider returned incomplete, refused, or non-text output.", usage)
            if any(part.get("type") != "text" for part in data.get("content", [])):
                raise InferenceError("Provider returned unsupported non-text output.", usage)
            result = "\n".join(part["text"] for part in data.get("content", []) if part.get("type") == "text")
        if not result.strip() or len(result) > MAX_OUTPUT_CHARS:
            raise InferenceError("Provider returned empty or overlong output.", usage)
        result = redact(result, credential)
        if len(result) > MAX_OUTPUT_CHARS:
            raise InferenceError("Provider returned overlong redacted output.", usage)
        return {"text": result, **usage}
    except (httpx.TimeoutException, TimeoutError):
        raise InferenceError("Provider inference timed out.", usage) from None
    except httpx.RequestError:
        raise InferenceError("Could not connect to the provider.", usage) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise InferenceError("Provider returned an invalid inference response.", usage) from None
