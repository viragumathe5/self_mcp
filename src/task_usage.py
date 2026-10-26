"""Reported token accounting and explicitly configured USD estimates; no guessed prices."""
from collections import defaultdict


def price_key(provider, model):
    return f"{provider}/{model}"


def summarize(run, ledger, prices):
    records = {}
    ranges = []
    start = 0
    for turn in [*run.get("turns", []), run]:
        end = run["usage"]["model_calls"] if turn is run else start + turn["usage"]["model_calls"]
        ranges.append((start, end, turn))
        for call in turn.get("call_details", []):
            records[str(call["call_id"])] = call
        start = end
    calls = []
    for number in range(1, run["usage"]["model_calls"] + 1):
        record = records.get(str(number), {})
        counts = ledger.get(str(number), {})
        provider, model = record.get("provider"), record.get("model")
        rate = prices.get(price_key(provider, model)) if provider and model else None
        input_tokens, output_tokens = counts.get("input_tokens"), counts.get("output_tokens")
        complete = input_tokens is not None and output_tokens is not None
        cost = ((input_tokens * rate["input_per_million"] + output_tokens * rate["output_per_million"]) / 1_000_000
                if complete and rate else None)
        calls.append({"call_id": number, "provider": provider, "model": model,
                      "stage": record.get("stage", "unknown"), "task_id": record.get("task_id"),
                      "input_tokens": input_tokens, "output_tokens": output_tokens,
                      "estimated_cost_usd": cost})

    def totals(items, usage=None):
        priced = [item["estimated_cost_usd"] for item in items if item["estimated_cost_usd"] is not None]
        return {**(usage or {
            "input_tokens": sum(item["input_tokens"] or 0 for item in items),
            "output_tokens": sum(item["output_tokens"] or 0 for item in items),
            "model_calls": len(items),
            "unknown_calls": sum(item["input_tokens"] is None or item["output_tokens"] is None for item in items),
        }), "estimated_cost_usd": sum(priced) if priced else (0 if not items else None),
            "priced_calls": len(priced), "unpriced_calls": len(items) - len(priced)}

    groups = defaultdict(list)
    for call in calls:
        groups[(call["provider"], call["model"])].append(call)
    return {"total": totals(calls, run["usage"]),
            "unattributed_tokens": {
                key: max(0, run["usage"][key] - sum(item[key] or 0 for item in calls))
                for key in ("input_tokens", "output_tokens")},
            "turns": [{"turn_id": turn.get("turn_id", turn.get("id")),
                       **totals(calls[begin:end])} for begin, end, turn in ranges],
            "models": [{"provider": provider, "model": model, **totals(items),
                        "rates": prices.get(price_key(provider, model)),
                        "stages": sorted({item["stage"] for item in items})}
                       for (provider, model), items in groups.items()], "calls": calls}


def project_totals(summaries):
    totals = [item["total"] for item in summaries]
    result = {key: sum(item[key] for item in totals) for key in (
        "input_tokens", "output_tokens", "model_calls", "unknown_calls", "priced_calls", "unpriced_calls")}
    costs = [item["estimated_cost_usd"] for item in totals if item["estimated_cost_usd"] is not None]
    result["estimated_cost_usd"] = sum(costs) if result["priced_calls"] else (0 if not result["model_calls"] else None)
    return result
