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


# ── utils ─────────────────────────────────────────────────────────────────────

@app.get("/api/token-count")
def token_count(text: str):
    return {"tokens": storage.count_tokens(text), "limit": storage.TOKEN_LIMIT}

def _require_store(store_id: str):
    if not storage.get_store(store_id):
        raise HTTPException(404, "Store not found")


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
