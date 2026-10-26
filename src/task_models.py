"""Conservative model discovery, strict lead schemas, and text-only inference."""

import asyncio
import json
import re
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool
import tokenization

import providers
import inference_policy


Provider = Annotated[str, StringConstraints(pattern=r"^(?:openai|anthropic|profile_[a-f0-9]{32}|auto)$")]
ModelID = Annotated[str, StringConstraints(max_length=200, pattern=f"^{providers.MODEL_PATTERN}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TaskID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,40}$")]
MAX_INPUT_TOKENS = 60000
MAX_INPUT_BYTES = 200000
MAX_OUTPUT_CHARS = 128000
LIMITATION = "This is model-based text/code drafting and review only. Code and simulations have not been executed; correctness has not been verified."


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelOptionsInput(StrictInput):
    provider: Provider


class RunInput(ModelOptionsInput):
    goal: Text = Field(max_length=12000)
    mode: Literal["delegate", "direct"] = "delegate"
    lead_model: ModelID | None = None
    worker_model: ModelID | None = None
    include_project_context: bool = False
    schema_retries: int = Field(default=0, ge=0, le=2)
    max_tasks: int = Field(default=6, ge=1, le=8)
    max_revisions: int = Field(default=2, ge=0, le=3)
    timeout_seconds: int | None = Field(default=None, ge=30, le=600)
    output_tokens: int | None = Field(default=None, ge=256, le=32768)
    reasoning_effort: Literal["auto", "low", "medium", "high"] = "auto"


class FollowupInput(RunInput):
    # Strings retain strict JSON validation and make MCP schemas portable.
    message_id: Annotated[str, StringConstraints(pattern=r"^[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$")]
    expected_turn_id: Annotated[str, StringConstraints(pattern=r"^[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$")]


class PlannedTask(StrictInput):
    id: TaskID
    title: Text = Field(max_length=200)
    instructions: Text = Field(max_length=6000)
    depends_on: list[TaskID] = Field(max_length=8)
    acceptance_criteria: list[Annotated[Text, Field(max_length=500)]] = Field(min_length=1, max_length=8)
    provider: Provider | None = None
    model: ModelID | None = None


class Plan(StrictInput):
    tasks: list[PlannedTask] = Field(min_length=1, max_length=8)


class TaskAssessment(StrictInput):
    task_id: TaskID
    verdict: Literal["keep", "revise"]
    reason: Text = Field(max_length=600)
    preserve: list[Annotated[Text, Field(max_length=300)]] = Field(default_factory=list, max_length=4)
    change: Annotated[Text, Field(max_length=1200)] | None = None
    criterion_index: int | None = Field(default=None, ge=1, le=8, description="For revise: the 1-based index of the existing unmet acceptance criterion. For keep: null.")
    evidence: Annotated[Text, Field(max_length=1200)] | None = Field(default=None, description="For revise: identify the concrete defect or missing content in this draft and its impact; not a style preference.")


class Review(StrictInput):
    task_reviews: list[TaskAssessment] = Field(default_factory=list, max_length=8)
    decisions: list[Annotated[Text, Field(max_length=400)]] = Field(default_factory=list, max_length=8)
    suggestions: list[Annotated[Text, Field(max_length=600)]] = Field(default_factory=list, max_length=8, description="Optional improvements that do not block approval or trigger revisions.")
    approved: bool
    feedback: Text = Field(max_length=6000)
    revision_tasks: list[TaskID] = Field(max_length=8)
    final_output: Annotated[Text, Field(max_length=120000)] | None


class CollaborativeReview(Review):
    # New model calls must account for every task; legacy saved reviews still load.
    task_reviews: list[TaskAssessment] = Field(min_length=1, max_length=8)


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


def validate_review(text: str, task_ids: set[str], *, require_assessments=False, allow_unapproved_output=False, task_criteria=None) -> Review:
    review = parse_json(text, CollaborativeReview if require_assessments else Review)
    revisions = set(review.revision_tasks)
    if len(revisions) != len(review.revision_tasks) or not revisions <= task_ids:
        raise ValueError("Invalid revision task IDs.")
    if review.approved:
        if revisions or not review.final_output:
            raise ValueError("Approval requires a final output and no revision tasks.")
    elif not revisions or (review.final_output is not None and not allow_unapproved_output):
        raise ValueError("Rejection requires revision tasks; final_output is allowed only on the final review.")
    if require_assessments or review.task_reviews:
        assessed = [item.task_id for item in review.task_reviews]
        if len(assessed) != len(set(assessed)) or set(assessed) != task_ids:
            raise ValueError("Review must assess every task exactly once.")
        if {item.task_id for item in review.task_reviews if item.verdict == "revise"} != revisions:
            raise ValueError("Task verdicts must match revision_tasks.")
        for item in review.task_reviews:
            if (item.verdict == "revise") != (item.change is not None):
                raise ValueError("Revisions require a concrete change; kept tasks must have null change.")
            if task_criteria is not None and item.verdict == "revise":
                if not item.criterion_index or item.criterion_index > len(task_criteria.get(item.task_id, [])) or not item.evidence:
                    raise ValueError("Revision requires an existing criterion_index and concrete evidence of a material defect.")
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
    def __init__(self, message, usage=None, *, kind=None):
        super().__init__(message)
        self.usage = usage if usage is not None else {}
        self.kind = kind


def request_input(instructions: str, payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    combined = instructions + "\n" + text
    if len(combined.encode("utf-8")) > MAX_INPUT_BYTES or len(tokenization.get_encoding("cl100k_base").encode(combined, disallowed_special=())) > MAX_INPUT_TOKENS:
        raise InferenceError("Model request exceeded the input safety budget; reduce the goal or task scope.")
    return text


async def infer(provider: str, credential: str, model: str, instructions: str, payload: dict, *, before_send=None, options=None) -> dict:
    providers.validate_provider(provider)
    settings = inference_policy.resolve(model, provider, overrides=options)
    text = await run_in_threadpool(request_input, instructions, payload)
    if before_send is not None:
        connection = await before_send()
        if connection.get("id") != provider or connection.get("kind") != provider or connection.get("local_only"):
            raise InferenceError("Selected fixed provider is not authorized for this request.")
        credential = connection.get("api_key")
        if not credential:
            raise InferenceError("Selected fixed provider has no configured credential.")
        if redact(model, credential) != model:
            raise InferenceError("Model identifiers must not contain credentials.")
        # The hook may have refreshed credentials and scrubbed the shared payload.
        # Revalidate synchronously: no awaited preparation may follow authorization.
        instructions = redact(instructions, credential)
        text = request_input(instructions, redact(payload, credential))
    if provider == "openai":
        url = "https://api.openai.com/v1/responses"
        headers = {"Authorization": f"Bearer {credential}"}
        body = {"model": model, "instructions": instructions, "input": text, "max_output_tokens": settings["output_tokens"], "store": False}
        if settings["reasoning_effort"]:
            body["reasoning"] = {"effort": settings["reasoning_effort"]}
    else:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": credential, "anthropic-version": "2023-06-01"}
        body = {"model": model, "system": instructions, "messages": [{"role": "user", "content": text}], "max_tokens": settings["output_tokens"]}
    usage = {}
    try:
        async with asyncio.timeout(settings["timeout_seconds"]), httpx.AsyncClient(timeout=settings["timeout_seconds"], follow_redirects=False, trust_env=False) as client:
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
            from provider_transport import _response_error
            try:
                diagnostic = _response_error(status, "POST /responses" if provider == "openai" else "POST /messages", json.loads(raw))
                kind = diagnostic.kind
            except (ValueError, TypeError):
                diagnostic = _response_error(status, "POST /responses" if provider == "openai" else "POST /messages")
                kind = None
            if status == 429:
                raise InferenceError(providers.limit_error_message(provider, raw, retry_after), usage, kind=kind)
            raise InferenceError(diagnostic.message, usage, kind=kind)
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
                if data.get("error"):
                    from provider_transport import _response_error
                    diagnostic = _response_error(200, "POST /responses", data)
                    raise InferenceError(diagnostic.message, usage, kind=diagnostic.kind)
                if data.get("incomplete_details", {}).get("reason") == "max_output_tokens":
                    raise InferenceError(f"Output token budget exhausted ({settings['output_tokens']} tokens, including reasoning). Increase the output budget or request a shorter answer.", usage, kind="output_limit")
                raise InferenceError("Provider returned incomplete or failed output.", usage)
            parts = [part for item in data.get("output", []) if item.get("type") == "message" for part in item.get("content", [])]
            if any(part.get("type") == "refusal" for part in parts):
                raise InferenceError("Provider refused the request.", usage)
            result = "\n".join(part["text"] for part in parts if part.get("type") == "output_text")
        else:
            if data.get("stop_reason") != "end_turn":
                if data.get("stop_reason") == "max_tokens":
                    raise InferenceError("Output token budget exhausted. Increase the output budget or request a shorter answer.", usage, kind="output_limit")
                raise InferenceError("Provider returned incomplete, refused, or non-text output.", usage)
            if any(part.get("type") != "text" for part in data.get("content", [])):
                raise InferenceError("Provider returned unsupported non-text output.", usage)
            result = "\n".join(part["text"] for part in data.get("content", []) if part.get("type") == "text")
        if not result.strip():
            raise InferenceError("Provider returned no answer text. Check the model's reasoning and output budgets.", usage)
        if len(result) > MAX_OUTPUT_CHARS:
            raise InferenceError(f"Answer exceeds the application's {MAX_OUTPUT_CHARS}-character limit. Request a shorter answer or split the request.", usage)
        result = redact(result, credential)
        if len(result) > MAX_OUTPUT_CHARS:
            raise InferenceError("Provider returned overlong redacted output.", usage)
        return {"text": result, **usage}
    except (httpx.TimeoutException, TimeoutError):
        raise InferenceError(f"Provider inference timed out after {settings['timeout_seconds']} seconds. Increase the call timeout or select a faster model.", usage, kind="timeout") from None
    except httpx.RequestError:
        raise InferenceError("Could not connect to the provider.", usage) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise InferenceError("Provider returned an invalid inference response.", usage) from None
