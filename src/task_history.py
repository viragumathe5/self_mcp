"""Build bounded conversation context while retaining every turn in persistence."""
import json
import tokenization

from providers import ProviderError


def conversation(run, instructions, payload, connection=None):
    turns = run.get("turns", [])
    if not turns:
        return None
    encoding = tokenization.get_encoding("cl100k_base")
    context_limit = min(60000, connection.get("context_tokens", 8192) - connection.get("output_tokens", 2048)) if connection else 60000
    # Match adapter framing conservatively; reserve capacity for current stage data.
    token_limit = context_limit - 256
    def fits(value):
        text = instructions + "\n" + json.dumps({**payload, "conversation": value}, ensure_ascii=False)
        return len(text.encode("utf-8")) <= 190000 and len(encoding.encode(text, disallowed_special=())) <= token_limit
    def entry(index):
        turn = turns[index]
        answer = turn.get("final_output")
        partials = [] if answer else [{"title": item["title"], "draft": item["output"]}
                                     for item in turn.get("tasks", []) if item.get("output")]
        return {"turn": index + 1, "user": turn["goal"], "assistant": answer,
                "status": turn["status"], **({"partial_drafts": partials} if partials else {})}
    def context(indices):
        return {"original_goal": run.get("root_goal", turns[0]["goal"]),
                "turns": [entry(index) for index in sorted(indices)],
                "omitted_older_turns": len(turns) - len(indices)}
    # Never drop the latest message or last successful result; they anchor a follow-up.
    required = {len(turns) - 1}
    completed = next((index for index in reversed(range(len(turns))) if turns[index].get("final_output")), None)
    if completed is not None:
        required.add(completed)
    if not fits(context(required)):
        raise ProviderError(422, "The latest conversation result and this request exceed the model's context budget. Shorten the request or select a connection with a larger context window; your history is saved.")
    selected = set(required)
    for index in reversed(range(len(turns))):
        if index not in selected and fits(context(selected | {index})):
            selected.add(index)
    return context(selected)
