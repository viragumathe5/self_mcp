"""Bounded, credential-isolated transports for explicitly configured profiles."""

import asyncio
from functools import lru_cache
import json
import re

import httpx
import tokenization
import model_policy
import inference_policy
from starlette.concurrency import run_in_threadpool

from providers import MODEL_PATTERN, ProviderError, normalize_base_url, validate_allowed_models, validate_metadata


REQUEST_TIMEOUT = 90.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_INPUT_BYTES = 200_000
MAX_TEXT_CHARS = 128_000
MAX_MODELS = 500

# Only these adapter-owned operations may appear in diagnostics, never the base URL.
OPERATIONS = {
    ("GET", "/api/tags"): "GET /api/tags",
    ("GET", "/models"): "GET /models",
    ("POST", "/api/show"): "POST /api/show",
    ("POST", "/api/chat"): "POST /api/chat",
    ("POST", "/chat/completions"): "POST /chat/completions",
}
ERROR_CATEGORIES = (
    ({"model_not_found", "model_not_available", "model_unavailable"},
     (r"model (?:[\"'][^\"'\r\n]{1,200}[\"'] )?(?:not found|is unavailable|is not available)(?:, try pulling it first)?[.!]?",),
     "Model not found or unavailable. Check that the selected model is installed and accessible."),
    ({"model_not_supported", "unsupported_model", "model_not_supported_for_chat", "unsupported_model_for_chat", "embedding_only_model"},
     (r"(?:model )?(?:[\"'][^\"'\r\n]{1,200}[\"'] )?does not support (?:chat|generate|generation)[.!]?",
      r"(?:this |the selected )?model (?:is (?:an? )?embeddings?-only(?: model)?|only supports embeddings)[.!]?",
      r"this is not a chat model and thus not supported in the v1/chat/completions endpoint\.(?: did you mean to use v1/completions\?)?"),
     "Model does not support chat/generation on this endpoint."),
    ({"context_length_exceeded", "context_window_exceeded"},
     (r"(?:input |prompt )?(?:context length|context window) exceeded[.!]?",
      r"this model's maximum context length is [0-9]{1,9} tokens\.(?: however, you requested [0-9]{1,9} tokens(?: \([0-9]{1,9} in the messages, [0-9]{1,9} in the completion\))?\.(?: please reduce the length of the messages or completion\.)?)?"),
     "Context length exceeded. Reduce the input or reserved output tokens."),
    ({"insufficient_quota", "insufficient_credits", "billing_hard_limit_reached", "billing_not_active", "credit_balance_too_low"},
     (r"(?:insufficient (?:quota|credits)|credit balance (?:is )?too low)[.!]?",
      r"you exceeded your current quota, please check your plan and billing details\.[ \t]*"),
     "Insufficient quota or credits; check provider billing."),
    ({"out_of_memory", "memory_allocation_failed", "insufficient_memory", "oom"},
     (r"(?:cuda (?:error: )?)?out of memory[.!]?",
      r"(?:failed to allocate (?:cuda |gpu |system )?memory|unable to allocate (?:cuda |gpu |system )?memory)[.!]?",
      r"model requires more (?:system |gpu )?memory(?: \([0-9.]+ [kmgt]?i?b\))? than is available(?: \([0-9.]+ [kmgt]?i?b\))?[.!]?"),
     "Memory allocation failed or out of memory (OOM). Check available RAM/VRAM or select a smaller model."),
    ({"rate_limit_exceeded", "rate_limit_error", "too_many_requests"},
     (r"(?:rate limit exceeded|too many requests)[.!]?",),
     "Rate limit reached. Check provider request/token limits."),
    ({"overloaded_error", "server_overloaded", "service_unavailable"},
     (r"(?:service unavailable|(?:the )?(?:server|service|model) is (?:currently )?overloaded|overloaded)[.!]?",),
     "Service unavailable or overloaded. Check provider availability."),
    ({"unsupported_parameter", "unsupported_value"}, (),
     "Model rejected a request parameter. Check its capability profile, reasoning setting, and API protocol."),
)


def _response_error(status, operation, result=None, note=None):
    """Translate untrusted error data into fixed text; never interpolate its strings."""
    result = result if isinstance(result, dict) else {}
    error = result.get("error")
    fields = error if isinstance(error, dict) else {}
    codes = [fields.get("code"), fields.get("type")]
    message = fields.get("message") if fields else error
    category = None
    kind = None
    kinds = ("model_unavailable", "unsupported_model", "context_length", "quota", "out_of_memory", "rate_limit", "overloaded", "unsupported_parameter")
    # Exact code/type matches take precedence over prose. Nested/unknown values are ignored.
    for index, (allowed, _, description) in enumerate(ERROR_CATEGORIES):
        if any(isinstance(code, str) and code in allowed for code in codes):
            category, kind = description, kinds[index]
            break
    if category is None and isinstance(message, str) and len(message) <= 2048:
        for index, (_, patterns, description) in enumerate(ERROR_CATEGORIES):
            if any(re.fullmatch(pattern, message.strip(), re.I) for pattern in patterns):
                category, kind = description, kinds[index]
                break
    if status == 401:
        category = "Authentication failed. Check the selected profile credential."
    elif status == 403:
        category = "Access denied. Check provider permissions for the selected model."
    elif status == 402:
        category = "Payment required; check provider credits and billing."
    elif 300 <= status < 400:
        category = "Provider redirected the request; redirects are not followed. Check the configured protocol."
    elif category is None:
        category = {429: "Rate limit reached. Check provider quota, billing, and rate limits.",
                    503: "Service unavailable or overloaded. Check provider availability."}.get(
                        status, "Provider request failed. Check model access, provider availability, and configured protocol.")
    if status in (401, 403, 402) or 300 <= status < 400:
        kind = None
    elif status == 503 and kind is None:
        kind = "overloaded"
    text = f"HTTP {status} ({operation}): {category}"
    if note:
        text += " " + note
    exception = ProviderError(401 if status in (401, 403) else 429 if status == 429 else 502, text, kind=kind)
    usage = {}
    for source in (fields, result):
        _usage(usage, source, "prompt_eval_count", "eval_count")
        _usage(usage, source.get("usage"), "prompt_tokens", "completion_tokens")
        _usage(usage, source.get("usage"), "input_tokens", "output_tokens")
    exception.usage = usage
    return exception


def _remote_model(item):
    if not isinstance(item, dict):
        return False
    if item.get("remote") or item.get("remote_host") or item.get("remote_model"):
        return True
    for field in ("id", "name", "model"):
        name = item.get(field)
        if isinstance(name, str) and (name.lower().endswith(":cloud") or name.lower().endswith("-cloud")):
            return True
    return any(_remote_model(item.get(field)) for field in ("data", "details", "model_info"))


def model_is_local(connection, item) -> bool:
    """Endpoint and reported model locality, not proof that an arbitrary proxy is local."""
    _, local = normalize_base_url(connection.get("base_url"))
    if not local or not isinstance(item, dict) or item.get("is_local") is False:
        return False
    return not _remote_model(item)


def _connection(connection):
    if not isinstance(connection, dict) or connection.get("kind") not in {"ollama", "openai_compatible"}:
        raise ProviderError(422, "Unsupported provider protocol. Choose Ollama or OpenAI-compatible.")
    base, local = normalize_base_url(connection.get("base_url"))
    if connection.get("local_only") and not local:
        raise ProviderError(422, "Local-only routing requires a loopback provider endpoint.")
    key = connection.get("api_key")
    if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[!-~]{1,4096}", key)):
        raise ProviderError(422, "Invalid provider credential.")
    if connection.get("requires_key") and not key:
        raise ProviderError(422, "This provider requires an API key. Re-enter its credential before connecting.")
    allowed = validate_allowed_models(connection.get("allowed_models", []))
    validate_metadata([base, *allowed], [key])
    return base, {"Authorization": f"Bearer {key}"} if key else {}


async def _request(connection, method, suffix, body=None, *, timeout=None):
    # Validate again immediately before sending; no task-supplied URL is accepted.
    base, headers = _connection(connection)
    operation = OPERATIONS.get((method, suffix), "provider operation")
    status = None
    request_timeout = timeout or REQUEST_TIMEOUT
    try:
        async with asyncio.timeout(request_timeout), httpx.AsyncClient(
            timeout=httpx.Timeout(request_timeout), follow_redirects=False, trust_env=False,
        ) as client:
            async with client.stream(method, base + suffix, headers=headers, json=body) as response:
                status = response.status_code
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        if status != 200:
                            raise _response_error(status, operation, note="Error body exceeded the 1 MiB safety limit.")
                        raise ProviderError(502, f"HTTP 200 ({operation}): Provider response exceeded the 1 MiB safety limit.")
                    raw.extend(chunk)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                # Successful metadata reads retain their existing validation and
                # authorization order; chat error envelopes get HTTP diagnostics too.
                if status != 200 or (suffix in ("/api/chat", "/chat/completions") and result.get("error")):
                    raise _response_error(status, operation, result)
                return result
    except (httpx.TimeoutException, TimeoutError):
        if status is not None and status != 200:
            raise _response_error(status, operation, note="Reading the error body timed out.") from None
        raise ProviderError(504, f"Provider request timed out after {request_timeout:g} seconds. Increase the call timeout or select a faster model.", kind="timeout") from None
    except httpx.RequestError:
        if status is not None and status != 200:
            raise _response_error(status, operation, note="Could not read the error body.") from None
        raise ProviderError(502, "Could not connect to the provider. Check its base URL, protocol, and availability.") from None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        if status is not None and status != 200:
            raise _response_error(status, operation, note="Error body was not a valid JSON object.") from None
        raise ProviderError(502, f"HTTP 200 ({operation}): Provider returned an invalid JSON response. Check the configured protocol.") from None


async def discover(connection) -> dict:
    _connection(connection)
    ollama = connection["kind"] == "ollama"
    result = await _request(connection, "GET", "/api/tags" if ollama else "/models")
    items = result.get("models" if ollama else "data")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ProviderError(502, "Provider returned an invalid model list. Check the configured protocol.")
    models = {}
    key = connection.get("api_key")
    allowed = set(connection.get("allowed_models", []))
    filtered = False
    for item in items[:MAX_MODELS]:
        identifier = item.get("model", item.get("name")) if ollama else item.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(MODEL_PATTERN, identifier) or (key and key in identifier):
            continue
        if allowed and identifier not in allowed:
            continue
        name = item.get("name", identifier) if ollama else item.get("name", item.get("display_name", identifier))
        if not isinstance(name, str) or not 1 <= len(name) <= 200 or not name.isprintable() or (key and key in name):
            name = identifier
        local = model_is_local(connection, item)
        if connection.get("local_only") and not local:
            filtered = True
            continue
        models[identifier] = {"id": identifier, "name": name, "is_local": local, "remote": _remote_model(item)}
        if type(item.get("chat_capable")) is bool:
            models[identifier]["chat_capable"] = item["chat_capable"]
    message = "Connection and model listing succeeded. This does not verify inference access or availability for any model."
    message += " Locality uses the endpoint and server-reported model metadata; a local proxy can still forward requests remotely."
    if any(item["remote"] for item in models.values()):
        message += " Cloud-backed models may send prompts to a remote service when explicitly selected."
    if filtered:
        message += " Cloud-backed models were excluded by local-only routing."
    if allowed:
        message += " Model listing is restricted to the profile's allowed models."
    if len(items) > MAX_MODELS:
        message += " Model listing was truncated at the safety limit."
    return {"models": list(models.values()), "message": message}


@lru_cache(maxsize=1)
def _encoding():
    return tokenization.get_encoding("cl100k_base")


def _messages(connection, model, instructions, payload):
    _connection(connection)
    if not isinstance(model, str) or not re.fullmatch(MODEL_PATTERN, model):
        raise ProviderError(422, "Choose a valid model identifier before inference.")
    _require_allowed_model(connection, model)
    if not model_policy.chat_compatible(model):
        raise ProviderError(422, "Selected model does not support chat on this endpoint.", kind="unsupported_model")
    context = connection.get("context_tokens", 8192)
    output = connection.get("output_tokens", 2048)
    if type(context) is not int or type(output) is not int or not 1 <= output < context <= 2_000_000:
        raise ProviderError(422, "Invalid provider context or output token limits.")
    try:
        if not isinstance(instructions, str):
            raise ValueError
        content = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": content}]
        encoded = json.dumps(messages, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_INPUT_BYTES:
            raise ProviderError(422, "Provider input exceeds the 200000-byte safety limit; reduce the input.")
        validate_metadata((model, instructions, content), [connection.get("api_key")])
        # Include a conservative allowance for message framing, without truncating input.
        tokens = sum(len(_encoding().encode(m["content"], disallowed_special=())) for m in messages) + 16
        if tokens > context - output:
            raise ProviderError(422, "Provider input exceeds the configured context budget after reserving output tokens; reduce the input.", kind="context_length")
        return messages, context, output
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderError(422, "Provider input must contain valid JSON and text instructions.") from None


def _text(message, connection):
    if not isinstance(message, dict) or message.get("tool_calls") or message.get("function_call") or message.get("refusal"):
        raise ProviderError(502, "Provider returned a refusal or tool call instead of a text answer.")
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise ProviderError(502, "Provider returned no usable answer text. Check the model's reasoning and output budgets.")
    if len(text) > MAX_TEXT_CHARS:
        raise ProviderError(502, f"Answer exceeds the application's {MAX_TEXT_CHARS}-character limit. Request a shorter answer or split the request.")
    if connection.get("api_key") and connection["api_key"] in text:
        raise ProviderError(502, "Provider returned unsafe answer text.")
    return text


def _usage(result, usage, input_key, output_key):
    if isinstance(usage, dict):
        for source, target in ((input_key, "input_tokens"), (output_key, "output_tokens")):
            value = usage.get(source)
            if type(value) is int and value >= 0:
                result[target] = value
    return result


def _fingerprint(connection):
    base, _ = _connection(connection)
    return (connection.get("id"), connection["kind"], base,
            connection.get("is_local", normalize_base_url(base)[1]), connection.get("requires_key", False),
            connection.get("context_tokens", 8192), connection.get("output_tokens", 2048),
            tuple(connection.get("allowed_models", [])))


async def _authorize(connection, before_send, model, messages):
    fingerprint = _fingerprint(connection)
    if before_send is not None:
        fresh = await before_send()
        if not isinstance(fresh, dict):
            raise ProviderError(409, "Provider authorization did not return a current connection.")
        fresh = dict(fresh)
        fresh["allowed_models"] = validate_allowed_models(fresh.get("allowed_models", []))
        # An omitted or relaxed flag cannot bypass a local-only check already required for this call.
        fresh["local_only"] = bool(connection.get("local_only") or fresh.get("local_only"))
        _connection(fresh)
        _require_allowed_model(fresh, model)
        if _fingerprint(fresh) != fingerprint:
            raise ProviderError(409, "Provider connection or token limits changed before sending. Retry with current settings.")
        connection = fresh
    validate_metadata([model, *(message["content"] for message in messages)], [connection.get("api_key")])
    _require_allowed_model(connection, model)
    return connection


def _require_allowed_model(connection, model):
    allowed = connection.get("allowed_models", [])
    if allowed and model not in allowed:
        raise ProviderError(422, "The selected model is not in this profile's allowed models.")


def _require_local_model(connection, item):
    if connection.get("local_only") and not model_is_local(connection, item):
        raise ProviderError(422, "Local-only routing cannot use a cloud-backed or remote model.")


async def infer(connection, model, instructions, payload, before_send=None, *, options=None) -> dict:
    connection = dict(connection)
    settings = inference_policy.resolve(model, connection["kind"], connection, options)
    connection["allowed_models"] = validate_allowed_models(connection.get("allowed_models", []))
    messages, context, output = await run_in_threadpool(_messages, connection, model, instructions, payload)
    connection = await _authorize(connection, before_send, model, messages)
    _require_local_model(connection, {"id": model})
    ollama = connection["kind"] == "ollama"
    if ollama and connection.get("local_only"):
        details = await _request(connection, "POST", "/api/show", {"model": model})
        connection = await _authorize(connection, before_send, model, messages)
        _require_local_model(connection, {"id": model, "data": details})
        if details.get("error") or not any(details.get(field) is not None for field in ("details", "model_info", "modelfile")):
            raise ProviderError(502, "Ollama did not return usable model metadata for local-only routing.")
    output = settings["output_tokens"]
    # Reserve the connection's configured budget even when this call requests less.
    body = {"model": model, "messages": messages, "stream": False}
    if ollama:
        body["options"] = {"num_ctx": context, "num_predict": output}
    else:
        # Reasoning chat models use the newer token field; keep generic local APIs compatible.
        field = "max_completion_tokens" if re.search(r"(?:^|/)(?:gpt-(?:[5-9]|[1-9]\d)|o[1-9]\d*(?:-|$))", model) else "max_tokens"
        body[field] = output
        if settings["reasoning_effort"]:
            body["reasoning_effort"] = settings["reasoning_effort"]
    response = await _request(connection, "POST", "/api/chat" if ollama else "/chat/completions", body, timeout=settings["timeout_seconds"])
    usage = _usage({}, response, "prompt_eval_count", "eval_count") if ollama else _usage({}, response.get("usage"), "prompt_tokens", "completion_tokens")
    def incomplete(message):
        error = ProviderError(502, message, kind="output_limit")
        error.usage = usage
        return error
    def answer(message):
        try:
            return {"text": _text(message, connection), **usage}
        except ProviderError as error:
            error.usage = usage
            raise
    if ollama:
        if response.get("done") is not True or response.get("done_reason", "stop") != "stop":
            raise incomplete(f"Provider did not finish a complete answer; check the output token budget ({output} tokens).")
        return answer(response.get("message"))
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        error = ProviderError(502, "Provider returned an invalid chat completion. Check the configured protocol.")
        error.usage = usage
        raise error
    choice = choices[0]
    if choice.get("finish_reason") != "stop" or choice.get("stop_reason") == "length" or response.get("stop_reason") == "length":
        if choice.get("finish_reason") == "length" or choice.get("stop_reason") == "length" or response.get("stop_reason") == "length":
            raise incomplete(f"Output token budget exhausted ({output} tokens, including reasoning). Increase the connection's output budget or request a shorter answer.")
        error = ProviderError(502, "Provider did not finish a complete text answer; check refusals, tool support, and the output token limit.")
        error.usage = usage
        raise error
    return answer(choice.get("message"))
