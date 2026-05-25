"""
api.py — FastAPI REST API, multi-store.
Runs on http://localhost:8000

Routes:
  GET/POST        /api/stores
  GET/PUT/DELETE  /api/stores/<store_id>
  GET/POST        /api/stores/<store_id>/documents
  GET/PUT/DELETE  /api/stores/<store_id>/documents/<doc_id>
  GET/POST        /api/stores/<store_id>/links
  DELETE          /api/stores/<store_id>/links/<link_id>
  GET/POST        /api/stores/<store_id>/issues
  GET/PUT/DELETE  /api/stores/<store_id>/issues/<issue_id>
  POST            /api/stores/<store_id>/issues/<issue_id>/assign
  GET             /api/token-count?text=...
"""
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import storage

app = FastAPI(title="self_mcp", version="0.2.0")
STATIC_DIR = Path(__file__).parent / "static"


# ── models ────────────────────────────────────────────────────────────────────

class StoreCreate(BaseModel):
    name: str
    description: str = ""

class StoreUpdate(BaseModel):
    name: str
    description: str = ""

class DocCreate(BaseModel):
    title: str
    content: str
    tags: list[str] = []

class DocUpdate(BaseModel):
    title: str
    content: str
    tags: list[str] = []

class LinkCreate(BaseModel):
    title: str
    url: str
    description: str = ""
    tags: list[str] = []


class IssueCreate(BaseModel):
    title: str
    description: str = ""
    status: str = "open"
    priority: str = "medium"
    tags: list[str] = []

class IssueUpdate(BaseModel):
    title: str
    description: str = ""
    status: str = "open"
    priority: str = "medium"
    tags: list[str] = []

class IssueAssign(BaseModel):
    assigned: bool
    task_doc_id: str | None = None


class PlanCreate(BaseModel):
    title: str
    idea: str
    subtasks: list[dict]
    max_tokens_per_subtask: int = 1500

class SubtaskComplete(BaseModel):
    output: str
    output_summary: str = ""

class SubtaskClaim(BaseModel):
    agent_name: str = "agent"

class PlanFinalOutput(BaseModel):
    output: str


# ── stores ────────────────────────────────────────────────────────────────────

@app.get("/api/stores")
def get_stores():
    return storage.list_stores()

@app.post("/api/stores", status_code=201)
def create_store(body: StoreCreate):
    return storage.create_store(body.name, body.description)

@app.get("/api/stores/{store_id}")
def get_store(store_id: str):
    s = storage.get_store(store_id)
    if not s:
        raise HTTPException(404, "Store not found")
    return s

@app.put("/api/stores/{store_id}")
def update_store(store_id: str, body: StoreUpdate):
    s = storage.update_store(store_id, body.name, body.description)
    if not s:
        raise HTTPException(404, "Store not found")
    return s

@app.delete("/api/stores/{store_id}", status_code=204)
def delete_store(store_id: str):
    if not storage.delete_store(store_id):
        raise HTTPException(404, "Store not found")


# ── documents ─────────────────────────────────────────────────────────────────

@app.get("/api/stores/{store_id}/documents")
def get_documents(store_id: str):
    _require_store(store_id)
    return storage.list_documents(store_id)

@app.post("/api/stores/{store_id}/documents", status_code=201)
def create_document(store_id: str, body: DocCreate):
    _require_store(store_id)
    try:
        return storage.create_document(store_id, body.title, body.content, body.tags)
    except ValueError as e:
        raise HTTPException(422, str(e))

@app.get("/api/stores/{store_id}/documents/{doc_id}")
def get_document(store_id: str, doc_id: str):
    doc = storage.get_document(store_id, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    return doc

@app.put("/api/stores/{store_id}/documents/{doc_id}")
def update_document(store_id: str, doc_id: str, body: DocUpdate):
    try:
        doc = storage.update_document(store_id, doc_id, body.title, body.content, body.tags)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not doc:
        raise HTTPException(404, "Document not found")
    return doc

@app.delete("/api/stores/{store_id}/documents/{doc_id}", status_code=204)
def delete_document(store_id: str, doc_id: str):
    if not storage.delete_document(store_id, doc_id):
        raise HTTPException(404, "Document not found")


# ── links ─────────────────────────────────────────────────────────────────────

@app.get("/api/stores/{store_id}/links")
def get_links(store_id: str):
    _require_store(store_id)
    return storage.list_links(store_id)

@app.post("/api/stores/{store_id}/links", status_code=201)
def create_link(store_id: str, body: LinkCreate):
    _require_store(store_id)
    try:
        return storage.create_link(store_id, body.title, body.url, body.description, body.tags)
    except ValueError as e:
        raise HTTPException(422, str(e))

@app.delete("/api/stores/{store_id}/links/{link_id}", status_code=204)
def delete_link(store_id: str, link_id: str):
    if not storage.delete_link(store_id, link_id):
        raise HTTPException(404, "Link not found")


# ── issues ────────────────────────────────────────────────────────────────────

@app.get("/api/stores/{store_id}/issues")
def get_issues(store_id: str):
    _require_store(store_id)
    return storage.list_issues(store_id)

@app.post("/api/stores/{store_id}/issues", status_code=201)
def create_issue(store_id: str, body: IssueCreate):
    _require_store(store_id)
    try:
        return storage.create_issue(
            store_id, body.title, body.description,
            body.status, body.priority, body.tags,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))

@app.get("/api/stores/{store_id}/issues/{issue_id}")
def get_issue(store_id: str, issue_id: str):
    issue = storage.get_issue(store_id, issue_id)
    if not issue:
        raise HTTPException(404, "Issue not found")
    return issue

@app.put("/api/stores/{store_id}/issues/{issue_id}")
def update_issue(store_id: str, issue_id: str, body: IssueUpdate):
    issue = storage.update_issue(
        store_id, issue_id, body.title, body.description,
        body.status, body.priority, body.tags,
    )
    if not issue:
        raise HTTPException(404, "Issue not found")
    return issue

@app.delete("/api/stores/{store_id}/issues/{issue_id}", status_code=204)
def delete_issue(store_id: str, issue_id: str):
    if not storage.delete_issue(store_id, issue_id):
        raise HTTPException(404, "Issue not found")

@app.post("/api/stores/{store_id}/issues/{issue_id}/assign")
def assign_issue(store_id: str, issue_id: str, body: IssueAssign):
    """Set or clear the assigned_to_model flag; record optional task_doc_id."""
    issue = storage.set_issue_assignment(
        store_id, issue_id, body.assigned, body.task_doc_id
    )
    if not issue:
        raise HTTPException(404, "Issue not found")
    return issue


# ── utils ─────────────────────────────────────────────────────────────────────

@app.get("/api/token-count")
def token_count(text: str):
    return {"tokens": storage.count_tokens(text), "limit": storage.TOKEN_LIMIT}

def _require_store(store_id: str):
    if not storage.get_store(store_id):
        raise HTTPException(404, "Store not found")


# ── plans ──────────────────────────────────────────────────────────────────────

@app.get("/api/stores/{store_id}/plans")
def get_plans(store_id: str):
    _require_store(store_id)
    return storage.list_plans(store_id)

@app.post("/api/stores/{store_id}/plans", status_code=201)
def create_plan(store_id: str, body: PlanCreate):
    _require_store(store_id)
    try:
        plan = storage.create_plan(
            store_id, body.title, body.idea,
            body.subtasks, body.max_tokens_per_subtask,
        )
        # Auto-create a context document per subtask so agents can read them via MCP
        for st in plan["subtasks"]:
            content = _build_subtask_doc(plan, st)
            try:
                doc = storage.create_document(
                    store_id,
                    title=f"[Plan] {plan['title']} — {st['title']}",
                    content=content,
                    tags=["plan", "subtask", "agent-task"],
                )
                storage.set_subtask_doc(store_id, plan["id"], st["id"], doc["id"])
            except ValueError:
                pass  # token limit hit — doc not created, agent will see raw subtask via tool
        return storage.get_plan(store_id, plan["id"])
    except ValueError as e:
        raise HTTPException(422, str(e))

@app.get("/api/stores/{store_id}/plans/{plan_id}")
def get_plan(store_id: str, plan_id: str):
    plan = storage.get_plan(store_id, plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    return plan

@app.delete("/api/stores/{store_id}/plans/{plan_id}", status_code=204)
def delete_plan(store_id: str, plan_id: str):
    plan = storage.get_plan(store_id, plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    # Delete all subtask context docs
    for st in plan.get("subtasks", []):
        if st.get("doc_id"):
            storage.delete_document(store_id, st["doc_id"])
    if not storage.delete_plan(store_id, plan_id):
        raise HTTPException(404, "Plan not found")

@app.get("/api/stores/{store_id}/plans/{plan_id}/next")
def get_next_subtask(store_id: str, plan_id: str):
    st = storage.get_next_subtask(store_id, plan_id)
    if not st:
        return {"message": "No pending subtasks available.", "subtask": None}
    return {"subtask": st}

@app.post("/api/stores/{store_id}/plans/{plan_id}/subtasks/{subtask_id}/claim")
def claim_subtask(store_id: str, plan_id: str, subtask_id: str, body: SubtaskClaim):
    st = storage.claim_subtask(store_id, plan_id, subtask_id, body.agent_name)
    if not st:
        raise HTTPException(404, "Subtask not found or already claimed")
    return st

@app.post("/api/stores/{store_id}/plans/{plan_id}/subtasks/{subtask_id}/complete")
def complete_subtask(store_id: str, plan_id: str, subtask_id: str, body: SubtaskComplete):
    st = storage.complete_subtask(store_id, plan_id, subtask_id, body.output, body.output_summary)
    if not st:
        raise HTTPException(404, "Subtask not found")
    return st

@app.post("/api/stores/{store_id}/plans/{plan_id}/finalize")
def finalize_plan(store_id: str, plan_id: str, body: PlanFinalOutput):
    plan = storage.set_plan_final_output(store_id, plan_id, body.output)
    if not plan:
        raise HTTPException(404, "Plan not found")
    return plan


def _build_subtask_doc(plan: dict, st: dict) -> str:
    deps = st.get("depends_on", [])
    dep_note = ""
    if deps:
        dep_titles = []
        for idx in deps:
            dep_st = next((s for s in plan["subtasks"] if s["index"] == idx), None)
            if dep_st:
                dep_titles.append(f"subtask {idx}: {dep_st['title']}")
        dep_note = f"\n**Depends on:** {', '.join(dep_titles)}\n"

    return f"""# {st['title']}

**Plan:** {plan['title']}  
**Subtask {st['index'] + 1} of {len(plan['subtasks'])}**  
**Subtask ID:** `{st['id']}`  
**Plan ID:** `{plan['id']}`  
{dep_note}
## Your task

{st['description']}

## Context & background

{st['context'] or '_No additional context provided._'}

## Instructions for the agent

You are working on one part of a larger plan. Your job is **only** this subtask.

1. Read the task and context above carefully.
2. Do the work for this subtask only — stay focused and don't exceed your scope.
3. When done, call the `complete_subtask` MCP tool with:
   - `plan_id`: `{plan['id']}`
   - `subtask_id`: `{st['id']}`
   - `output`: your full result / deliverable
   - `output_summary`: a 1-2 sentence summary of what you produced (for the orchestrator)
4. The orchestrator will combine all subtask outputs into the final result.

## Overall goal (for context only)

{plan['idea']}
"""


# ── static / GUI ──────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/", include_in_schema=False)
def root():
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.get("/themes", include_in_schema=False)
def themes():
    return FileResponse(str(STATIC_DIR / "themes.html"))

# Catch-all: serve the SPA for any non-API path so browser back/forward works
@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str):
    if full_path.startswith("api/") or full_path.startswith("static/"):
        raise HTTPException(404)
    return FileResponse(str(STATIC_DIR / "index.html"))
