"""Cheap, deterministic model suitability rules. Rankings are hints, not benchmarks.

Keep endpoint compatibility separate from model size: many local *instruct* models
serve chat, while OpenAI's gpt-3.5-turbo-instruct uses legacy completions only.
"""
import re


NON_CHAT = re.compile(
    r"embed|rerank|whisper|moderation|(?:^|[-_/:])(?:audio|tts|image|dall-e|stable-diffusion|realtime|transcribe|transcription)(?:[-_/:.]|$)", re.I)
LEGACY_COMPLETION = re.compile(
    r"(?:^|/)(?:gpt-3\.5-turbo-instruct(?:-|$)|(?:text-|code-)?(?:ada|babbage|curie|davinci)(?:-|$))", re.I)


def chat_compatible(model, metadata=None):
    if NON_CHAT.search(model) or LEGACY_COMPLETION.search(model):
        return False
    metadata = metadata or {}
    # Honor explicit server capability declarations; absent metadata remains unknown.
    if metadata.get("chat_capable") is False:
        return False
    return True


def parameter_size(model):
    match = re.search(r"(?:^|[^a-z0-9])(\d+(?:\.\d+)?)b(?:[^a-z]|$)", model.lower())
    return float(match[1]) if match else 0


def traits(model):
    name = model.lower()
    size = parameter_size(name)
    small = any(part in name for part in ("mini", "nano", "haiku", "small")) or 0 < size <= 8
    coding = bool(re.search(r"coder|codestral|codeqwen|starcoder|deepseek-coder|(?:^|[-/])code(?:-|$)", name))
    reasoning = bool(re.search(r"(?:^|/)o[1-9]\d*(?:-|$)|deepseek-r1|qwq|reason", name))
    known = bool(re.search(r"gpt-|claude-|llama|qwen|gemma|mistral|phi-|deepseek|codestral|starcoder|qwq", name))
    legacy = bool(re.search(r"gpt-3\.5|(?:^|/)gpt-4(?:-|$)|claude-[12](?:-|$)", name))
    strength = 4 if "opus" in name or reasoning else 3 if "sonnet" in name or re.search(r"gpt-[5-9]|gpt-[1-9]\d", name) else 2
    if small:
        strength = min(strength, 2)
    if size >= 30:
        strength = max(strength, 3)
    if legacy:
        strength = 1
    version = tuple(int(x) for x in re.findall(r"\d+", name) if len(x) < 6)
    return {"small": small, "coding": coding, "reasoning": reasoning, "known": known,
            "legacy": legacy, "strength": strength, "size": size, "version": version}


def task_kind(task=None, role="worker"):
    if role != "worker":
        return "reasoning"
    task = task or {}
    text = " ".join(str(task.get(key, "")) for key in ("title", "instructions", "acceptance_criteria")).lower()
    # Match words, so incidental substrings do not classify a task.
    if re.search(r"\b(prove|proof|derive|derivation|theorem|optimi[sz]ation|mathematical|reasoning|architecture|trade.?offs)\b", text):
        return "reasoning"
    if re.search(r"\b(code|coding|implement|implementation|debug|python|javascript|typescript|sql|api|unit tests?|refactor|docker|environment|dependencies|setup)\b", text):
        return "coding"
    if re.search(r"\b(summar(?:y|ies|ize|ise)|extract|classify|classification|format|reformat|translate|translation|rewrite|proofread|tag|label)\b", text):
        return "light"
    return "general"


def rank(candidate, kind, mode="balanced"):
    t = traits(candidate["model"])
    local = candidate.get("is_local") is True if mode in ("local_only", "local_first") else False
    if kind == "coding":
        fit = (t["coding"], t["strength"], t["known"], t["version"], t["size"])
    elif kind == "reasoning" or mode == "quality":
        fit = (t["strength"], t["reasoning"], t["known"], t["version"], t["size"])
    else:
        # Prefer a known compact model. Unknown size does not mean cheap or capable.
        fit = (t["small"], t["known"], not t["reasoning"], -t["size"], t["version"])
    return local, not t["legacy"], fit, candidate["model"], candidate.get("provider", "")


def choose(candidates, kind, mode="balanced"):
    return max(candidates, key=lambda item: rank(item, kind, mode)) if candidates else None


def reason(kind):
    return {"coding": "Coding task: prefer a coding specialist or capable general model.",
            "reasoning": "Planning/reasoning task: prefer a stronger reasoning model.",
            "light": "Extraction or writing task: prefer a compact model.",
            "general": "General task: use the validated planner route or a compact default."}[kind]
