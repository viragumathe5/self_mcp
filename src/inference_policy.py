"""Model request capabilities, independent of task planning and ranking.

Repository-owned configuration only: model output cannot change these settings.
Unknown models keep adapter defaults and receive no guessed reasoning parameters.
"""
import json
from functools import lru_cache
from pathlib import Path
import re

from providers import ProviderError


@lru_cache(maxsize=1)
def profiles():
    return json.loads(Path(__file__).with_name("model_capabilities.json").read_text())["profiles"]


def resolve(model, kind, connection=None, overrides=None):
    connection, overrides = connection or {}, overrides or {}
    profile = next((item for item in profiles() if kind in item["kinds"] and re.search(item["pattern"], model, re.I)), {})
    custom = kind in {"ollama", "openai_compatible"}
    output = min(connection.get("output_tokens", 2048), 32768) if custom else profile.get("output_tokens", 6000)
    timeout = overrides.get("timeout_seconds") or profile.get("timeout_seconds", 90)
    output = overrides.get("output_tokens") or output
    minimum = 256 if overrides.get("output_tokens") is not None else 1
    if type(timeout) is not int or not 30 <= timeout <= 600 or type(output) is not int or not minimum <= output <= 32768:
        raise ProviderError(422, "Choose a timeout of 30–600 seconds and an output budget of 256–32768 tokens.")
    if custom and output > connection.get("output_tokens", 2048):
        raise ProviderError(422, "Output budget exceeds this connection's configured limit. Increase it in Connections or choose a smaller run budget.")
    effort = overrides.get("reasoning_effort", "auto")
    if effort == "auto":
        effort = profile.get("reasoning_default")
    elif effort not in profile.get("reasoning_efforts", []):
        raise ProviderError(422, "This model has no configured support for that reasoning setting. Choose Automatic reasoning or update its capability profile.")
    return {"timeout_seconds": timeout, "output_tokens": output, "reasoning_effort": effort,
            "profile": profile.get("name", "Adapter defaults")}


def overrides(run):
    return {key: run[key] for key in ("timeout_seconds", "output_tokens", "reasoning_effort")
            if run.get(key) is not None and run.get(key) != "auto"}


def next_action(message):
    """Fixed actionable advice; never expose provider payloads or credentials."""
    value = message.lower()
    if "invalid review" in value:
        return "The lead could not produce a valid review. Use the saved worker drafts; send a follow-up or select another lead model in Settings."
    if "character" in value:
        return "Request a shorter answer or split the request into smaller pieces."
    if "timed out" in value or "timeout" in value:
        return "Increase the call timeout in Settings, reduce the request, or select a faster model."
    if "output" in value and any(word in value for word in ("budget", "incomplete", "token", "overlong", "oversized")):
        return "Request a shorter answer or raise the output budget in Settings. For custom endpoints, also check the connection's token limits."
    if any(word in value for word in ("quota", "credit", "billing", "payment")):
        return "Check this provider's API credits and billing before starting another run."
    if any(word in value for word in ("protocol", "http 404")):
        return "Check the exact model ID and API protocol in Connections, or leave model selection on Automatic."
    if any(word in value for word in ("authentication", "credential", "denied", "permission", "access")):
        return "Check the selected connection's credential and permission to use this model."
    if any(word in value for word in ("context", "input", "too large")):
        return "Shorten the request or attached knowledge, or use a connection with a larger context limit."
    if any(word in value for word in ("model", "protocol", "parameter")):
        return "Check the exact model ID and API protocol in Connections, or leave model selection on Automatic."
    return "Your saved work is retained. Send a follow-up to continue this task, or open work details to inspect the model and failure."
