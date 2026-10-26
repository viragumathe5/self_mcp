"""Bounded routing policy. Only settings supply connections; models supply offered IDs.

Snapshots contain public metadata and endpoint fingerprints, never credentials. They
live with the runner job, so interrupted runs cannot replay old authorization.
"""

import hashlib
import json
import re

import provider_api
import providers
import task_models as models
import model_policy as policy


FIXED = frozenset({"openai", "anthropic"})
KINDS = frozenset({"openai", "anthropic", "ollama", "openai_compatible"})
MODES = frozenset({"local_only", "local_first", "balanced", "quality"})
MAX_CONNECTIONS = 20
MAX_MODELS = 500
MAX_CANDIDATES = 6
PROMPT_LIMIT = 12


def scrub(value, secrets):
    for secret in sorted(secrets, key=len, reverse=True):
        value = models.redact(value, secret)
    return models.redact(value, "")


def preferences():
    value = provider_api.settings.routing()
    if value.get("mode") not in MODES or not isinstance(value.get("providers"), list):
        raise providers.ProviderError(422, "Invalid routing preferences. Configure allowed providers first.")
    return value


def rows():
    # status() resolves credentials (including remote keyrings). Routing must use
    # metadata only until the allowlist and locality policy have been applied.
    return {row["id"]: row for row in provider_api.settings.catalog()}


def fingerprint(connection):
    # Include limits and locality as well as the endpoint. Credential rotation is allowed.
    public = {key: connection.get(key) for key in (
        "id", "kind", "base_url", "is_local", "context_tokens", "output_tokens", "requires_key")}
    public["allowed_models"] = connection.get("allowed_models", [])
    return hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()


def public_profile(row):
    return {**{key: row.get(key) for key in ("id", "name", "kind", "is_local", "default_model")},
            "allowed_models": list(row.get("allowed_models", []))}


def snapshot(provider):
    """Resolve only metadata-approved connections; local_only never resolves cloud keys."""
    if provider in FIXED:
        # Fixed explicit execution deliberately retains credential_for/list_models/infer.
        return {"mode": "explicit", "profiles": {}, "fingerprints": {}}, set()
    auto = provider == "auto"
    pref = preferences() if auto else {"mode": "explicit", "providers": [provider]}
    allowed = list(dict.fromkeys(pref["providers"]))
    if len(allowed) > MAX_CONNECTIONS:
        raise providers.ProviderError(422, "Routing allows at most 20 connections. Reduce the allowlist.")
    if not allowed:
        raise providers.ProviderError(422, "Auto routing requires an explicit provider allowlist. Configure routing first.")
    catalog = rows()
    selected, fingerprints, secrets, warnings = {}, {}, set(), []
    for provider_id in allowed:
        row = catalog.get(provider_id)
        if not row or row.get("kind") not in KINDS:
            if not auto:
                raise providers.ProviderError(422 if row else 404, "Provider profile is missing or not configured.")
            warnings.append("An allowed provider is unavailable or unsupported and was excluded.")
            continue
        # This test MUST precede connection() (including credential resolution).
        if pref["mode"] == "local_only" and row.get("is_local") is not True:
            continue
        try:
            connection = provider_api.settings.connection(provider_id)
        except providers.ProviderError:
            if not auto:
                raise providers.ProviderError(422, "Provider connection is not configured; check its settings and credential.") from None
            warnings.append("An allowed provider connection could not be resolved and was excluded.")
            continue
        if connection.get("id") != provider_id or connection.get("kind") not in KINDS:
            raise providers.ProviderError(422, "Invalid provider connection configuration.")
        if pref["mode"] == "local_only" and connection.get("is_local") is not True:
            raise providers.ProviderError(422, "Local-only routing rejected a non-local connection.")
        if connection.get("api_key"):
            secrets.add(connection["api_key"])
        selected[provider_id] = public_profile(row)
        fingerprints[provider_id] = fingerprint(connection)
    if not selected:
        raise providers.ProviderError(422, "No configured supported providers are allowed by this routing mode. Configure a local profile or explicitly allow remote providers.")
    return {"mode": pref["mode"], "profiles": scrub(selected, secrets),
            "fingerprints": fingerprints, "warnings": warnings}, secrets


def connection_for(provider, selected):
    """Revoke before resolution/network; never expand the submission trust snapshot."""
    if provider not in selected["fingerprints"]:
        raise providers.ProviderError(422, "Provider was not authorized for this run.")
    row = rows().get(provider)
    if not row or row.get("kind") not in KINDS:
        raise providers.ProviderError(422, "Selected provider was removed or disabled; execution stopped.")
    local_only = selected["mode"] == "local_only"
    if selected["mode"] != "explicit":
        pref = preferences()
        local_only = local_only or pref["mode"] == "local_only"
        if provider not in pref["providers"] or (
            local_only and row.get("is_local") is not True
        ):
            raise providers.ProviderError(422, "Routing authorization was revoked; execution stopped without rerouting.")
    try:
        connection = provider_api.settings.connection(provider)
    except providers.ProviderError:
        raise providers.ProviderError(422, "Selected provider connection is unavailable; execution stopped.") from None
    if fingerprint(connection) != selected["fingerprints"][provider]:
        raise providers.ProviderError(422, "Selected provider configuration changed; execution stopped without rerouting.")
    # This runtime restriction is not endpoint configuration and is deliberately
    # excluded from the fingerprint. Adapters must enforce it per model as well.
    return {**connection, "local_only": local_only}


def size(model):
    match = re.search(r"(?:^|[^a-z0-9])(\d+(?:\.\d+)?)b(?:[^a-z]|$)", model.lower())
    return float(match[1]) if match else 0


def quality(model):
    name = model.lower()
    tier = (4 if "opus" in name else 3 if "sonnet" in name else
            1 if any(word in name for word in ("haiku", "mini", "nano")) else
            3 if re.match(r"(?:gpt-[5-9]|o[3-9])", name) else 2)
    return tier, size(model), model


def custom_options(discovered, profile, local_only=False):
    # Unknown chat aliases are allowed; known incompatible endpoints are excluded.
    items = discovered.get("models", []) if isinstance(discovered, dict) else discovered
    allowed = set(profile.get("allowed_models", []))
    catalog = {}
    for item in items[:MAX_MODELS]:
        model = item.get("id")
        is_local = profile.get("is_local") is True and item.get("is_local") is True
        if local_only and not is_local:
            continue
        if (isinstance(model, str) and (not allowed or model in allowed)
                and re.fullmatch(providers.MODEL_PATTERN, model) and policy.chat_compatible(model, item)):
            catalog[model] = {"id": model, "name": str(item.get("name") or model)[:200], "is_local": is_local}
    values = sorted(catalog.values(), key=lambda item: item["id"])
    default = profile.get("default_model")
    pool = [{"model": name, **item} for name, item in catalog.items()]
    lead = default if default in catalog else (policy.choose(pool, "reasoning") or {}).get("model")
    worker = (policy.choose(pool, "light") or {}).get("model")
    return {"models": values, "lead_model": lead, "worker_model": worker,
            "reason": "HEURISTIC: compatible stored default or stronger lead; compact workers. Task rules select coding, reasoning or lightweight models. Discovery does not prove inference access or price."}


def auto_options(selected, options, warnings):
    candidates, leads, workers = [], [], []
    mode = selected["mode"]
    for provider, result in options.items():
        profile = selected["profiles"][provider]
        indexed = {item["id"]: item for item in result["models"]
                   if mode != "local_only" or item.get("is_local") is True}
        recommended = [result["lead_model"], result["worker_model"], profile.get("default_model")]
        if mode == "local_first":
            local = [name for name, item in indexed.items() if item.get("is_local") is True]
            if local:
                # Keep a local lead in the bounded catalog even if cloud aliases
                # dominate the endpoint's size/family rankings and stored default.
                recommended.insert(0, max(local, key=lambda name: (
                    name == profile.get("default_model"), quality(name))))
        typed_pool = [{"model": name, **item} for name, item in indexed.items()]
        recommended += [(policy.choose(typed_pool, kind, mode) or {}).get("model")
                        for kind in ("coding", "reasoning", "light")]
        ids = list(dict.fromkeys([name for name in recommended if name in indexed] +
                                sorted(indexed, key=quality, reverse=True)))[:MAX_CANDIDATES]
        pool = [{"provider": provider, "model": name, "name": indexed[name]["name"],
                 "is_local": indexed[name].get("is_local") is True and profile["is_local"] is True} for name in ids]
        candidates.extend(pool)
        lead_model = result["lead_model"]
        if mode == "balanced" and profile.get("default_model") in ids:
            lead_model = profile["default_model"]
        elif mode == "quality" and provider not in FIXED and pool:
            lead_model = max(pool, key=lambda item: quality(item["model"]))["model"]
        elif mode == "local_first":
            local = [item for item in pool if item["is_local"]]
            if local:
                lead_model = max(local, key=lambda item: (
                    item["model"] == profile.get("default_model"), quality(item["model"])))["model"]
        leads.extend(item for item in pool if item["model"] == lead_model)
        workers.extend(pool)

    def rank(item, worker=False):
        profile = selected["profiles"][item["provider"]]
        result = options[item["provider"]]
        preferred = item["model"] == (result["worker_model"] if worker else profile.get("default_model"))
        local = item["is_local"] if mode in ("local_only", "local_first") else False
        return (local, quality(item["model"]) if mode == "quality" else preferred, quality(item["model"]))

    leads = sorted(leads, key=rank, reverse=True)[:PROMPT_LIMIT]
    workers = sorted(workers, key=lambda item: rank(item, True), reverse=True)[:PROMPT_LIMIT]
    if not leads or not workers:
        raise providers.ProviderError(422, "No compatible generation models were discovered on allowed providers.")
    lead, worker = leads[0], workers[0]
    specialists = [worker] + [policy.choose(candidates, kind, mode) for kind in ("coding", "reasoning", "light")]
    workers = list({(item["provider"], item["model"]): item for item in specialists + workers if item}.values())[:PROMPT_LIMIT]
    reason = "HEURISTIC task-based routing from approved providers. Automatically chosen models can recover from recognized model failures using approved alternatives; no price or quality guarantee."
    if mode == "local_first":
        reason += " Local providers are preferred; explicitly allowed remote workers may receive task data and incur charges."
    elif mode == "local_only":
        reason += " Local-only: remote connections are excluded before discovery."
    return {"models": options[lead["provider"]]["models"][:MAX_MODELS],
            "lead_model": lead["model"], "worker_model": worker["model"],
            "lead_provider": lead["provider"], "worker_provider": worker["provider"],
            "mode": mode, "candidates": candidates, "worker_candidates": workers,
            "warnings": selected.get("warnings", []) + warnings, "reason": reason}


def assign(plan, default_provider, default_model, candidates, *, adaptive=False, mode="balanced"):
    offered = {(item["provider"], item["model"]) for item in candidates}
    for task in plan.tasks:
        pair = (task.provider or default_provider, task.model or default_model)
        if pair not in offered:
            raise ValueError("Plan selected a route outside the offered candidate pairs.")
        kind = policy.task_kind(task.model_dump())
        if adaptive and kind != "general":
            selected = policy.choose(candidates, kind, mode)
            pair = selected["provider"], selected["model"]
        task.provider, task.model = pair
