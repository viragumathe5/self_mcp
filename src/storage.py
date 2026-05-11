"""
storage.py — multi-store JSON persistence.
Schema: { "stores": [ { id, name, slug, description, documents: [], links: [], issues: [] } ] }
Stored at ~/.self_mcp/data.json

Auto-migrates old single-store format on first load.
"""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path.home() / ".self_mcp"
DATA_FILE = DATA_DIR / "data.json"
TOKEN_LIMIT = 2000


# ── helpers ───────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    """Convert a store name to a URL-safe slug."""
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "_", s)
    s = re.sub(r"^-+|-+$", "", s)
    return s or "store"


def count_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── load / save ───────────────────────────────────────────────────────────────

def _load() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        default = _new_store("Personal", "Default personal knowledge store")
        data = {"stores": [default]}
        _save(data)
        return data
    raw = json.loads(DATA_FILE.read_text())
    # ── migrate old format ────────────────────────────────────────────────────
    if "stores" not in raw:
        store = _new_store("Personal", "Migrated from previous version")
        store["documents"] = raw.get("documents", [])
        store["links"] = raw.get("links", [])
        data = {"stores": [store]}
        _save(data)
        return data
    return raw


def _save(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2))


def _new_store(name: str, description: str = "") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "slug": _slug(name),
        "description": description,
        "created_at": _now(),
        "documents": [],
        "links": [],
        "issues": [],
    }


# ── stores ────────────────────────────────────────────────────────────────────

def list_stores() -> list[dict]:
    data = _load()
    # Return stores without the full doc/link bodies for list views
    result = []
    for s in data["stores"]:
        result.append({
            "id": s["id"],
            "name": s["name"],
            "slug": s["slug"],
            "description": s.get("description", ""),
            "created_at": s.get("created_at", ""),
            "doc_count": len(s.get("documents", [])),
            "link_count": len(s.get("links", [])),
            "total_tokens": sum(d.get("tokens", 0) for d in s.get("documents", [])),
            "issue_count": len(s.get("issues", [])),
        })
    return result


def get_store(store_id: str) -> Optional[dict]:
    return next((s for s in _load()["stores"] if s["id"] == store_id), None)


def get_store_by_slug(slug: str) -> Optional[dict]:
    return next((s for s in _load()["stores"] if s["slug"] == slug), None)


def create_store(name: str, description: str = "") -> dict:
    data = _load()
    slug = _slug(name)
    # ensure slug uniqueness
    existing_slugs = {s["slug"] for s in data["stores"]}
    base = slug
    i = 2
    while slug in existing_slugs:
        slug = f"{base}_{i}"
        i += 1
    store = _new_store(name, description)
    store["slug"] = slug
    data["stores"].append(store)
    _save(data)
    return _store_summary(store)


def update_store(store_id: str, name: str, description: str = "") -> Optional[dict]:
    data = _load()
    for s in data["stores"]:
        if s["id"] == store_id:
            s["name"] = name
            s["description"] = description
            _save(data)
            return _store_summary(s)
    return None


def delete_store(store_id: str) -> bool:
    data = _load()
    before = len(data["stores"])
    data["stores"] = [s for s in data["stores"] if s["id"] != store_id]
    if len(data["stores"]) < before:
        _save(data)
        return True
    return False


def _store_summary(s: dict) -> dict:
    return {
        "id": s["id"],
        "name": s["name"],
        "slug": s["slug"],
        "description": s.get("description", ""),
        "created_at": s.get("created_at", ""),
        "doc_count": len(s.get("documents", [])),
        "link_count": len(s.get("links", [])),
        "total_tokens": sum(d.get("tokens", 0) for d in s.get("documents", [])),
        "issue_count": len(s.get("issues", [])),
    }


# ── documents (scoped to store) ───────────────────────────────────────────────

def list_documents(store_id: str) -> list[dict]:
    s = get_store(store_id)
    return s["documents"] if s else []


def get_document(store_id: str, doc_id: str) -> Optional[dict]:
    return next((d for d in list_documents(store_id) if d["id"] == doc_id), None)


def create_document(store_id: str, title: str, content: str, tags: list[str] = []) -> dict:
    tokens = count_tokens(content)
    if tokens > TOKEN_LIMIT:
        raise ValueError(f"Content exceeds {TOKEN_LIMIT}-token limit (~{tokens} tokens).")
    data = _load()
    store = next((s for s in data["stores"] if s["id"] == store_id), None)
    if not store:
        raise ValueError("Store not found.")
    doc = {
        "id": str(uuid.uuid4()),
        "title": title,
        "content": content,
        "tags": tags,
        "tokens": tokens,
        "created_at": _now(),
        "updated_at": _now(),
    }
    store["documents"].append(doc)
    _save(data)
    return doc


def update_document(store_id: str, doc_id: str, title: str, content: str, tags: list[str] = []) -> Optional[dict]:
    tokens = count_tokens(content)
    if tokens > TOKEN_LIMIT:
        raise ValueError(f"Content exceeds {TOKEN_LIMIT}-token limit (~{tokens} tokens).")
    data = _load()
    store = next((s for s in data["stores"] if s["id"] == store_id), None)
    if not store:
        return None
    for doc in store["documents"]:
        if doc["id"] == doc_id:
            doc.update({"title": title, "content": content, "tags": tags,
                         "tokens": tokens, "updated_at": _now()})
            _save(data)
            return doc
    return None


def delete_document(store_id: str, doc_id: str) -> bool:
    data = _load()
    store = next((s for s in data["stores"] if s["id"] == store_id), None)
    if not store:
        return False
    before = len(store["documents"])
    store["documents"] = [d for d in store["documents"] if d["id"] != doc_id]
    if len(store["documents"]) < before:
        _save(data)
        return True
    return False


# ── links (scoped to store) ───────────────────────────────────────────────────

def list_links(store_id: str) -> list[dict]:
    s = get_store(store_id)
    return s["links"] if s else []


def create_link(store_id: str, title: str, url: str, description: str = "", tags: list[str] = []) -> dict:
    data = _load()
    store = next((s for s in data["stores"] if s["id"] == store_id), None)
    if not store:
        raise ValueError("Store not found.")
    link = {
        "id": str(uuid.uuid4()),
        "title": title,
        "url": url,
        "description": description,
        "tags": tags,
        "created_at": _now(),
    }
    store["links"].append(link)
    _save(data)
    return link


def delete_link(store_id: str, link_id: str) -> bool:
    data = _load()
    store = next((s for s in data["stores"] if s["id"] == store_id), None)
    if not store:
        return False
    before = len(store["links"])
    store["links"] = [l for l in store["links"] if l["id"] != link_id]
    if len(store["links"]) < before:
        _save(data)
        return True
    return False


# ── search (used by MCP tools) ────────────────────────────────────────────────

def search_documents(store_id: str, query: str) -> list[dict]:
    q = query.lower()
    return [d for d in list_documents(store_id)
            if q in d["title"].lower() or q in d["content"].lower()
            or any(q in t.lower() for t in d.get("tags", []))]


def search_links(store_id: str, query: str) -> list[dict]:
    q = query.lower()
    return [l for l in list_links(store_id)
            if q in l["title"].lower() or q in l["url"].lower()
            or q in l.get("description", "").lower()]


# ── issues (scoped to store) ──────────────────────────────────────────────────

def list_issues(store_id: str) -> list[dict]:
    s = get_store(store_id)
    return s.get("issues", []) if s else []


def get_issue(store_id: str, issue_id: str) -> Optional[dict]:
    return next((i for i in list_issues(store_id) if i["id"] == issue_id), None)


def create_issue(
    store_id: str,
    title: str,
    description: str = "",
    status: str = "open",
    priority: str = "medium",
    tags: list[str] = [],
) -> dict:
    data = _load()
    store = next((s for s in data["stores"] if s["id"] == store_id), None)
    if not store:
        raise ValueError("Store not found.")
    if "issues" not in store:
        store["issues"] = []
    issue = {
        "id": str(uuid.uuid4()),
        "title": title,
        "description": description,
        "status": status,           # open | in_progress | done
        "priority": priority,       # low | medium | high
        "tags": tags,
        "assigned_to_model": False,
        "task_doc_id": None,        # id of auto-created task document, if any
        "created_at": _now(),
        "updated_at": _now(),
    }
    store["issues"].append(issue)
    _save(data)
    return issue


def update_issue(
    store_id: str,
    issue_id: str,
    title: str,
    description: str = "",
    status: str = "open",
    priority: str = "medium",
    tags: list[str] = [],
) -> Optional[dict]:
    data = _load()
    store = next((s for s in data["stores"] if s["id"] == store_id), None)
    if not store:
        return None
    for issue in store.get("issues", []):
        if issue["id"] == issue_id:
            issue.update({
                "title": title,
                "description": description,
                "status": status,
                "priority": priority,
                "tags": tags,
                "updated_at": _now(),
            })
            _save(data)
            return issue
    return None


def set_issue_assignment(
    store_id: str,
    issue_id: str,
    assigned: bool,
    task_doc_id: Optional[str] = None,
) -> Optional[dict]:
    """Toggle the assigned_to_model flag; optionally record the task doc id."""
    data = _load()
    store = next((s for s in data["stores"] if s["id"] == store_id), None)
    if not store:
        return None
    for issue in store.get("issues", []):
        if issue["id"] == issue_id:
            issue["assigned_to_model"] = assigned
            issue["task_doc_id"] = task_doc_id
            issue["updated_at"] = _now()
            _save(data)
            return issue
    return None


def delete_issue(store_id: str, issue_id: str) -> bool:
    data = _load()
    store = next((s for s in data["stores"] if s["id"] == store_id), None)
    if not store:
        return False
    before = len(store.get("issues", []))
    store["issues"] = [i for i in store.get("issues", []) if i["id"] != issue_id]
    if len(store["issues"]) < before:
        _save(data)
        return True
    return False
