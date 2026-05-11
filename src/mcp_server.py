"""
mcp_server.py — per-store MCP SSE server.
Each store gets its own endpoint: http://localhost:8001/<store_slug>/sse

Discovery endpoint: GET http://localhost:8001/stores
  Returns JSON list of { name, slug, url } for all stores.

Documents are exposed as:
  - Tools      (list/get/search)
  - Resources  (doc://<id>)
  - Prompts    (one prompt per doc — so AI clients inject them as instructions)
"""
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import storage
import json

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import (
    Tool, TextContent,
    Resource, ReadResourceResult, TextResourceContents,
    Prompt, PromptMessage, GetPromptResult,
)

import uvicorn

logger = logging.getLogger(__name__)


# ── per-store MCP server factory ──────────────────────────────────────────────

def make_mcp_server(store_id: str, store_name: str) -> Server:
    srv = Server(f"self_mcp/{store_name}")

    # ── Tools ─────────────────────────────────────────────────────────────────

    @srv.list_tools()
    async def list_tools():
        return [
            Tool(name="list_documents",
                 description=f"List all documents in the '{store_name}' store.",
                 inputSchema={"type": "object", "properties": {}}),
            Tool(name="get_document",
                 description="Get full content of a document by ID.",
                 inputSchema={"type": "object", "properties": {
                     "id": {"type": "string"}}, "required": ["id"]}),
            Tool(name="search_documents",
                 description=f"Search documents in '{store_name}' by keyword.",
                 inputSchema={"type": "object", "properties": {
                     "query": {"type": "string"}}, "required": ["query"]}),
            Tool(name="list_links",
                 description=f"List all saved links in '{store_name}'.",
                 inputSchema={"type": "object", "properties": {}}),
            Tool(name="search_links",
                 description=f"Search links in '{store_name}' by keyword.",
                 inputSchema={"type": "object", "properties": {
                     "query": {"type": "string"}}, "required": ["query"]}),
            Tool(name="list_issues",
                 description=f"List all issues in '{store_name}', including status, priority, and whether they are assigned to the AI model.",
                 inputSchema={"type": "object", "properties": {}}),
            Tool(name="get_assigned_issues",
                 description=f"List only the issues in '{store_name}' that are assigned to the AI model and need to be worked on.",
                 inputSchema={"type": "object", "properties": {}}),
        ]

    @srv.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name == "list_documents":
            docs = storage.list_documents(store_id)
            if not docs:
                return [TextContent(type="text", text="No documents in this store.")]
            lines = [
                f"- **{d['title']}** (id: `{d['id']}`, tokens: {d['tokens']}, tags: {', '.join(d['tags']) or 'none'})"
                for d in docs
            ]
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "get_document":
            doc = storage.get_document(store_id, arguments["id"])
            if not doc:
                return [TextContent(type="text", text="Document not found.")]
            return [TextContent(type="text", text=f"# {doc['title']}\n\n{doc['content']}")]

        if name == "search_documents":
            docs = storage.search_documents(store_id, arguments["query"])
            if not docs:
                return [TextContent(type="text", text=f"No results for '{arguments['query']}'.")]
            parts = []
            for d in docs:
                snippet = d["content"][:300] + ("…" if len(d["content"]) > 300 else "")
                parts.append(f"## {d['title']}\n_id: {d['id']}_\n\n{snippet}")
            return [TextContent(type="text", text="\n\n---\n\n".join(parts))]

        if name == "list_links":
            links = storage.list_links(store_id)
            if not links:
                return [TextContent(type="text", text="No links in this store.")]
            lines = [
                f"- **{l['title']}**: {l['url']}" + (f" — {l['description']}" if l.get("description") else "")
                for l in links
            ]
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "search_links":
            links = storage.search_links(store_id, arguments["query"])
            if not links:
                return [TextContent(type="text", text=f"No links matched '{arguments['query']}'.")]
            lines = [
                f"- **{l['title']}**: {l['url']}" + (f" — {l['description']}" if l.get("description") else "")
                for l in links
            ]
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "list_issues":
            issues = storage.list_issues(store_id)
            if not issues:
                return [TextContent(type="text", text="No issues in this project.")]
            lines = []
            for i in issues:
                assigned = " 🤖 **[ASSIGNED TO MODEL]**" if i.get("assigned_to_model") else ""
                lines.append(
                    f"- [{i['status'].upper()}] **{i['title']}** "
                    f"(priority: {i['priority']}, id: `{i['id']}`){assigned}"
                )
                if i.get("description"):
                    lines.append(f"  {i['description'][:120]}{'…' if len(i.get('description','')) > 120 else ''}")
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "get_assigned_issues":
            issues = [i for i in storage.list_issues(store_id) if i.get("assigned_to_model")]
            if not issues:
                return [TextContent(type="text", text="No issues currently assigned to the model.")]
            parts = []
            for i in issues:
                parts.append(
                    f"## {i['title']}\n"
                    f"**Status:** {i['status']} | **Priority:** {i['priority']}\n"
                    f"**Tags:** {', '.join(i.get('tags', [])) or 'none'}\n\n"
                    f"{i.get('description', '_No description provided._')}"
                )
            return [TextContent(type="text", text="\n\n---\n\n".join(parts))]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    # ── Resources ─────────────────────────────────────────────────────────────

    @srv.list_resources()
    async def list_resources():
        return [
            Resource(
                uri=f"doc://{d['id']}",
                name=d["title"],
                description=f"Tags: {', '.join(d['tags']) or 'none'} | Tokens: {d['tokens']}",
                mimeType="text/markdown",
            )
            for d in storage.list_documents(store_id)
        ]

    @srv.read_resource()
    async def read_resource(uri: str):
        doc_id = uri.replace("doc://", "")
        doc = storage.get_document(store_id, doc_id)
        if not doc:
            raise ValueError(f"Resource not found: {uri}")
        return ReadResourceResult(
            contents=[TextResourceContents(
                uri=uri, mimeType="text/markdown",
                text=f"# {doc['title']}\n\n{doc['content']}"
            )]
        )

    # ── Prompts — each document is exposed as a named prompt ──────────────────
    # This is the primary mechanism for AI clients to receive doc contents
    # as instructions. Clients that support MCP prompts will see these.

    @srv.list_prompts()
    async def list_prompts():
        docs = storage.list_documents(store_id)
        prompts = [
            Prompt(
                name=f"doc_{d['id']}",
                description=f"[{store_name}] {d['title']}" + (
                    f" — tags: {', '.join(d['tags'])}" if d.get("tags") else ""
                ),
            )
            for d in docs
        ]
        # Add a special prompt listing all issues assigned to the model
        assigned = [i for i in storage.list_issues(store_id) if i.get("assigned_to_model")]
        if assigned:
            prompts.append(Prompt(
                name="assigned_issues",
                description=f"[{store_name}] Issues assigned to the AI model — work on these",
            ))
        return prompts

    @srv.get_prompt()
    async def get_prompt(name: str, arguments: dict | None = None):
        # Assigned-issues summary prompt
        if name == "assigned_issues":
            assigned = [i for i in storage.list_issues(store_id) if i.get("assigned_to_model")]
            if not assigned:
                raise ValueError("No issues currently assigned to the model.")
            lines = [
                f"The following issues in the '{store_name}' project have been assigned to you "
                f"by the user. Please work on them:\n"
            ]
            for i in assigned:
                lines.append(
                    f"### {i['title']}\n"
                    f"- **Status:** {i['status']}\n"
                    f"- **Priority:** {i['priority']}\n"
                    f"- **Tags:** {', '.join(i.get('tags', [])) or 'none'}\n"
                    f"\n{i.get('description', '_No description provided._')}\n"
                )
            return GetPromptResult(
                description=f"Issues assigned to AI — {store_name}",
                messages=[PromptMessage(
                    role="user",
                    content=TextContent(type="text", text="\n".join(lines)),
                )],
            )
        # Doc prompt
        doc_id = name.removeprefix("doc_")
        doc = storage.get_document(store_id, doc_id)
        if not doc:
            raise ValueError(f"Prompt not found: {name}")
        return GetPromptResult(
            description=f"{doc['title']} ({store_name})",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"# {doc['title']}\n\n{doc['content']}"
                    ),
                )
            ],
        )

    return srv


# ── ASGI app — routes all stores dynamically ──────────────────────────────────

# One SSE transport per store slug (created lazily as requests arrive)
_transports: dict[str, SseServerTransport] = {}
_servers: dict[str, Server] = {}


def _get_store_handler(slug: str):
    """Return (transport, server) for a slug, loading fresh from storage each time."""
    store = storage.get_store_by_slug(slug)
    if not store:
        return None, None
    store_id = store["id"]
    if slug not in _transports:
        _transports[slug] = SseServerTransport(f"/{slug}/messages/")
    # Always rebuild server so new docs are picked up on every connection
    _servers[slug] = make_mcp_server(store_id, store["name"])
    return _transports[slug], _servers[slug]


async def asgi_app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    if scope["type"] != "http":
        return

    path: str = scope.get("path", "")
    method: str = scope.get("method", "GET")

    # ── GET /stores — discovery ───────────────────────────────────────────────
    if path == "/stores" and method == "GET":
        stores = storage.list_stores()
        body = json.dumps([
            {"name": s["name"], "slug": s["slug"],
             "description": s.get("description", ""),
             "url": f"http://localhost:8001/{s['slug']}/sse"}
            for s in stores
        ]).encode()
        await _respond(send, 200, body, "application/json")
        return

    # ── GET /health ───────────────────────────────────────────────────────────
    if path == "/health":
        await _respond(send, 200, b"ok")
        return

    # ── /<slug>/sse — SSE connection ──────────────────────────────────────────
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[1] == "sse" and method == "GET":
        slug = parts[0]
        transport, server = _get_store_handler(slug)
        if not transport:
            await _respond(send, 404, f"No store with slug '{slug}'".encode())
            return
        async with transport.connect_sse(scope, receive, send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())
        return

    # ── /<slug>/messages/ — POST from MCP client ──────────────────────────────
    if len(parts) >= 2 and parts[1] == "messages" and method == "POST":
        slug = parts[0]
        transport, _ = _get_store_handler(slug)
        if not transport:
            await _respond(send, 404, f"No store with slug '{slug}'".encode())
            return
        await transport.handle_post_message(scope, receive, send)
        return

    await _respond(send, 404, b"Not found")


async def _respond(send, status: int, body: bytes, content_type: str = "text/plain"):
    await send({"type": "http.response.start", "status": status,
                "headers": [[b"content-type", content_type.encode()],
                             [b"content-length", str(len(body)).encode()],
                             [b"access-control-allow-origin", b"*"]]})
    await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":
    stores = storage.list_stores()
    print("\n[self_mcp MCP server]")
    print(f"  Discovery: http://localhost:8001/stores")
    for s in stores:
        print(f"  Store '{s['name']}': http://localhost:8001/{s['slug']}/sse")
    print()
    uvicorn.run(asgi_app, host="0.0.0.0", port=8001, log_level="warning")
