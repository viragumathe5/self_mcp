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
            # ── Plan / agent coordination tools ──────────────────────────────
            Tool(name="create_plan",
                 description=(
                     f"Decompose a goal into subtasks and store the plan in '{store_name}'. "
                     "IMPORTANT: You must populate the `subtasks` array yourself RIGHT NOW — do NOT call this tool with an empty subtasks list and do NOT ask the user for more information. "
                     "Your job is to act as a senior software engineer: read the idea, break it into 3-8 focused, independently-workable subtasks, and call this tool immediately with all subtasks filled in. "
                     "Each subtask must have a clear `title`, a precise `description` of exactly what to produce, and a `context` field with any background the agent needs. "
                     "Use `depends_on` (list of 0-based indexes) only when a subtask genuinely cannot start before another finishes. "
                     "After this tool returns, immediately call `get_next_task` with the returned plan_id and start working on the first subtask without any further prompting."
                 ),
                 inputSchema={
                     "type": "object",
                     "properties": {
                         "title":      {"type": "string", "description": "Short name for the plan (5 words max)"},
                         "idea":       {"type": "string", "description": "The full goal/idea being decomposed"},
                         "subtasks":   {
                             "type": "array",
                             "description": "3-8 subtasks YOU decide right now. Must not be empty.",
                             "items": {
                                 "type": "object",
                                 "properties": {
                                     "title":       {"type": "string", "description": "Short action-oriented title"},
                                     "description": {"type": "string", "description": "Exactly what to produce — be specific and complete"},
                                     "context":     {"type": "string", "description": "Background, constraints, tech stack, acceptance criteria"},
                                     "depends_on":  {"type": "array", "items": {"type": "integer"},
                                                     "description": "0-based indexes of subtasks that MUST finish first"},
                                 },
                                 "required": ["title", "description"],
                             },
                         },
                         "max_tokens_per_subtask": {"type": "integer", "description": "Target max tokens per subtask context doc (default 1500)"},
                     },
                     "required": ["title", "idea", "subtasks"],
                 }),
            Tool(name="list_plans",
                 description=f"List all decomposition plans in '{store_name}' with their status and subtask progress.",
                 inputSchema={"type": "object", "properties": {}}),
            Tool(name="get_plan",
                 description="Get full details of a plan including all subtasks and their outputs.",
                 inputSchema={"type": "object", "properties": {
                     "plan_id": {"type": "string"}}, "required": ["plan_id"]}),
            Tool(name="get_next_task",
                 description=(
                     "Claim and return the next available subtask to work on. "
                     "Call this immediately after create_plan, and again after each complete_subtask call. "
                     "Do NOT wait for user input between subtasks — just call this and start working on whatever it returns."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "plan_id":    {"type": "string"},
                     "agent_name": {"type": "string", "description": "Identifier for this agent session (optional)"},
                 }, "required": ["plan_id"]}),
            Tool(name="complete_subtask",
                 description=(
                     "Mark the current subtask as done and store your output. "
                     "Call this as soon as you finish the work for a subtask — do not summarise to the user first. "
                     "After this returns, immediately call get_next_task to pick up the next one."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "plan_id":        {"type": "string"},
                     "subtask_id":     {"type": "string"},
                     "output":         {"type": "string", "description": "Full result / deliverable for this subtask (code, text, decisions, etc.)"},
                     "output_summary": {"type": "string", "description": "1-2 sentence summary for the orchestrator"},
                 }, "required": ["plan_id", "subtask_id", "output"]}),
            Tool(name="get_plan_status",
                 description="Check completion status of all subtasks in a plan. Poll this to know when all parallel subagents are done.",
                 inputSchema={"type": "object", "properties": {
                     "plan_id": {"type": "string"}}, "required": ["plan_id"]}),
            Tool(name="finalize_plan",
                 description=(
                     "Store the final merged output for a completed plan. "
                     "Call this after all subtasks are done and you have synthesised their outputs into the final deliverable."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "plan_id": {"type": "string"},
                     "output":  {"type": "string", "description": "The final synthesised output combining all subtask results"},
                 }, "required": ["plan_id", "output"]}),
            Tool(name="spawn_subagent",
                 description=(
                     "Spawn an independent parallel worker agent to complete one subtask. "
                     "Call this once per ready subtask SIMULTANEOUSLY — do not await one before spawning the next. "
                     "The worker runs in the background; poll get_plan_status to know when it finishes. "
                     "Only spawn subtasks whose depends_on are already done."
                 ),
                 inputSchema={"type": "object", "properties": {
                     "plan_id":    {"type": "string", "description": "The plan this subtask belongs to"},
                     "subtask_id": {"type": "string", "description": "The specific subtask ID to work on"},
                     "store_slug": {"type": "string", "description": "The project slug (e.g. 'pizza')"},
                 }, "required": ["plan_id", "subtask_id", "store_slug"]}),
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

        # ── Plan tools ────────────────────────────────────────────────────────

        if name == "create_plan":
            title    = arguments.get("title", "Untitled Plan")
            idea     = arguments.get("idea", "")
            subtasks = arguments.get("subtasks", [])
            max_tok  = arguments.get("max_tokens_per_subtask", 1500)
            if not subtasks:
                return [TextContent(type="text", text=(
                    "ERROR: You called create_plan with an empty subtasks list. "
                    "You must decompose the idea into 3-8 subtasks yourself and pass them in the same call. "
                    "Do not ask the user — just decide the breakdown and call create_plan again with subtasks filled in."
                ))]
            # Check if there's an existing plan with this title and no subtasks — update it instead of duplicating
            existing_plans = storage.list_plans(store_id)
            existing = next((p for p in existing_plans if p["title"] == title and len(p["subtasks"]) == 0), None)
            if existing:
                # Delete the empty stub and recreate with subtasks
                storage.delete_plan(store_id, existing["id"])
            try:
                plan = storage.create_plan(store_id, title, idea, subtasks, max_tok)
                # Create context docs for each subtask
                import sys, os
                sys.path.insert(0, os.path.dirname(__file__))
                from api import _build_subtask_doc
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
                        pass
                plan = storage.get_plan(store_id, plan["id"])
                lines = [f"✅ Plan **{plan['title']}** created with {len(plan['subtasks'])} subtasks:\n"]
                for i, st in enumerate(plan["subtasks"]):
                    dep_str = f" (after subtasks {st['depends_on']})" if st.get("depends_on") else ""
                    lines.append(f"{i+1}. **{st['title']}**{dep_str}")
                lines.append(f"\n**Plan ID:** `{plan['id']}`")
                lines.append(f"\nNow call get_next_task with plan_id=`{plan['id']}` and start working immediately.")
                return [TextContent(type="text", text="\n".join(lines))]
            except ValueError as e:
                return [TextContent(type="text", text=f"Error: {e}")]

        if name == "list_plans":
            plans = storage.list_plans(store_id)
            if not plans:
                return [TextContent(type="text", text="No plans yet.")]
            lines = []
            for p in plans:
                done  = sum(1 for st in p["subtasks"] if st["status"] == "done")
                total = len(p["subtasks"])
                lines.append(
                    f"- **{p['title']}** [{p['status'].upper()}] "
                    f"{done}/{total} subtasks done | id: `{p['id']}`"
                )
                if total == 0:
                    lines.append(
                        f"  ⚠️  No subtasks yet. Call `create_plan` NOW with:\n"
                        f"  - title: \"{p['title']}\"\n"
                        f"  - idea: \"{p['idea']}\"\n"
                        f"  - subtasks: [your breakdown — do not ask the user, decide yourself]\n"
                        f"  - plan_id to replace: `{p['id']}`"
                    )
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "get_plan":
            plan = storage.get_plan(store_id, arguments["plan_id"])
            if not plan:
                return [TextContent(type="text", text="Plan not found.")]
            lines = [f"# {plan['title']}\n**Status:** {plan['status']}\n\n**Idea:** {plan['idea']}\n"]
            if not plan["subtasks"]:
                lines.append(
                    f"⚠️  This plan has NO subtasks yet.\n\n"
                    f"You must call `create_plan` RIGHT NOW with:\n"
                    f"- title: \"{plan['title']}\"\n"
                    f"- idea: \"{plan['idea']}\"\n"
                    f"- subtasks: your breakdown into 3-8 focused tasks (decide yourself, do NOT ask the user)\n\n"
                    f"The existing plan id `{plan['id']}` will be replaced automatically."
                )
            else:
                lines.append("## Subtasks\n")
                for st in plan["subtasks"]:
                    status_icon = {"done": "✅", "in_progress": "🔄", "pending": "⏳", "blocked": "🚫"}.get(st["status"], "⏳")
                    lines.append(f"{status_icon} **{st['index']+1}. {st['title']}** [{st['status']}]")
                    if st.get("output_summary"):
                        lines.append(f"   → {st['output_summary']}")
                if plan.get("final_output"):
                    lines.append(f"\n## Final Output\n{plan['final_output']}")
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "get_next_task":
            plan_id    = arguments["plan_id"]
            agent_name = arguments.get("agent_name", "agent")
            st = storage.get_next_subtask(store_id, plan_id)
            if not st:
                plan = storage.get_plan(store_id, plan_id)
                if plan and plan["status"] == "done":
                    return [TextContent(type="text", text="🎉 All subtasks are complete. Use the `orchestrator_synthesis` MCP prompt to produce the final output.")]
                return [TextContent(type="text", text="No pending subtasks available right now. Some may be in_progress by other agents, or all are done.")]
            # Claim it
            storage.claim_subtask(store_id, plan_id, st["id"], agent_name)
            plan = storage.get_plan(store_id, plan_id)
            lines = [
                f"# Your task: {st['title']}",
                f"**Subtask {st['index']+1} of {len(plan['subtasks'])}**",
                f"**Subtask ID:** `{st['id']}`  **Plan ID:** `{plan_id}`",
                f"\n## What to do\n{st['description']}",
            ]
            if st.get("context"):
                lines.append(f"\n## Context\n{st['context']}")
            if st.get("depends_on"):
                done_outputs = []
                for idx in st["depends_on"]:
                    dep = next((s for s in plan["subtasks"] if s["index"] == idx), None)
                    if dep and dep.get("output_summary"):
                        done_outputs.append(f"- Subtask {idx} ({dep['title']}): {dep['output_summary']}")
                if done_outputs:
                    lines.append(f"\n## Outputs from prerequisite subtasks\n" + "\n".join(done_outputs))
            lines.append(f"\n## When done\nCall `complete_subtask` with plan_id=`{plan_id}`, subtask_id=`{st['id']}`, your output and a short summary.")
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "complete_subtask":
            plan_id    = arguments["plan_id"]
            subtask_id = arguments["subtask_id"]
            output     = arguments["output"]
            summary    = arguments.get("output_summary", "")
            st = storage.complete_subtask(store_id, plan_id, subtask_id, output, summary)
            if not st:
                return [TextContent(type="text", text="Subtask not found.")]
            plan = storage.get_plan(store_id, plan_id)
            done  = sum(1 for s in plan["subtasks"] if s["status"] == "done")
            total = len(plan["subtasks"])
            msg = f"✅ Subtask **{st['title']}** marked done. {done}/{total} subtasks complete."
            if done == total:
                msg += "\n\n🎉 **All subtasks done!** Use the `orchestrator_synthesis` MCP prompt to synthesise the final output."
            else:
                next_st = storage.get_next_subtask(store_id, plan_id)
                if next_st:
                    msg += f"\n\nNext available: **{next_st['title']}** — call `get_next_task` to pick it up."
            return [TextContent(type="text", text=msg)]

        if name == "get_plan_status":
            plan = storage.get_plan(store_id, arguments["plan_id"])
            if not plan:
                return [TextContent(type="text", text="Plan not found.")]
            lines = [f"**{plan['title']}** — {plan['status'].upper()}\n"]
            for st in plan["subtasks"]:
                icon = {"done": "✅", "in_progress": "🔄", "pending": "⏳", "blocked": "🚫"}.get(st["status"], "⏳")
                agent = f" (agent: {st['assigned_agent']})" if st.get("assigned_agent") else ""
                lines.append(f"{icon} {st['index']+1}. {st['title']}{agent}")
            done  = sum(1 for s in plan["subtasks"] if s["status"] == "done")
            total = len(plan["subtasks"])
            lines.append(f"\n{done}/{total} subtasks complete.")
            if done == total and total > 0:
                lines.append("✅ All done — call `finalize_plan` with the merged output.")
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "finalize_plan":
            plan = storage.set_plan_final_output(store_id, arguments["plan_id"], arguments["output"])
            if not plan:
                return [TextContent(type="text", text="Plan not found.")]
            return [TextContent(type="text", text=f"✅ Plan **{plan['title']}** finalised and marked done.")]

        if name == "spawn_subagent":
            import subprocess, shutil
            plan_id    = arguments["plan_id"]
            subtask_id = arguments["subtask_id"]
            slug       = arguments["store_slug"]

            opencode_bin = shutil.which("opencode") or "/opt/homebrew/bin/opencode"
            message = (
                f"You are a worker agent. Your only job is to complete ONE subtask. "
                f"Connect to the self_mcp MCP server for project '{slug}'. "
                f"Call get_next_task with plan_id='{plan_id}' and agent_name='worker-{subtask_id[:8]}'. "
                f"Do the work described. "
                f"Call complete_subtask with plan_id='{plan_id}', subtask_id='{subtask_id}', your output, and a short summary. "
                f"Then stop — do not do anything else."
            )
            # Fire-and-forget: Popen returns immediately, worker runs in background
            proc = subprocess.Popen(
                [opencode_bin, "run", "--agent", "worker", "--dangerously-skip-permissions", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return [TextContent(type="text", text=
                f"🚀 Worker spawned (pid {proc.pid}) for subtask `{subtask_id[:8]}…` in plan `{plan_id[:8]}…`. "
                f"Poll `get_plan_status` with plan_id=`{plan_id}` to track progress."
            )]

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
        # Add assigned-issues prompt
        assigned = [i for i in storage.list_issues(store_id) if i.get("assigned_to_model")]
        if assigned:
            prompts.append(Prompt(
                name="assigned_issues",
                description=f"[{store_name}] Issues assigned to the AI model — work on these",
            ))
        # Add autonomous agent prompt whenever there are plans
        plans = storage.list_plans(store_id)
        if plans:
            prompts.append(Prompt(
                name="plan_agent",
                description=f"[{store_name}] Autonomous plan agent — your operating instructions for working on plans",
            ))
        return prompts

    @srv.get_prompt()
    async def get_prompt(name: str, arguments: dict | None = None):
        # Autonomous plan agent instructions
        if name == "plan_agent":
            plans = storage.list_plans(store_id)
            active = [p for p in plans if p["status"] in ("active", "in_progress", "draft")]
            plan_lines = []
            for p in active:
                done  = sum(1 for st in p["subtasks"] if st["status"] == "done")
                total = len(p["subtasks"])
                if total == 0:
                    plan_lines.append(
                        f"### {p['title']} (id: `{p['id']}`)\n"
                        f"**Status:** needs decomposition — 0 subtasks\n"
                        f"**Idea:** {p['idea']}\n"
                        f"→ Call `create_plan` with this title, this idea, and YOUR subtask breakdown right now."
                    )
                else:
                    plan_lines.append(
                        f"### {p['title']} (id: `{p['id']}`)\n"
                        f"**Status:** {p['status']} — {done}/{total} subtasks done\n"
                        f"**Idea:** {p['idea']}\n"
                        f"→ Call `get_next_task` with plan_id=`{p['id']}` and start working."
                    )
            plan_summary = "\n\n".join(plan_lines) if plan_lines else "_No active plans._"
            return GetPromptResult(
                description=f"Autonomous plan agent instructions — {store_name}",
                messages=[PromptMessage(
                    role="user",
                    content=TextContent(type="text", text=f"""# Autonomous Plan Agent — {store_name}

You are an autonomous orchestrator. Work through plans completely on your own. Never ask the user for permission or clarification between steps.

## Output directory

All generated files MUST be saved to:
  `/Users/vumathe/personal/self_mcp/output/{store_id}/`

- Create this directory if it doesn't exist.
- Each plan gets its own subfolder: `output/{store_id}/<plan_slug>/`
- NEVER save to /tmp or any other location.
- When editing an existing file, read it first, make surgical edits, preserve all existing structure. Do NOT rewrite from scratch.
- If a file has scroll issues or broken layout, fix only the CSS/HTML causing that — do not touch unrelated sections.

## Fork-join execution model

When you call `create_plan` and get back N subtasks:

1. **FORK** — Spawn one subagent per subtask that has no unmet dependencies. Run them ALL in parallel simultaneously — do not wait for one to finish before starting the next.
2. Each subagent works independently: reads its subtask via `get_next_task`, does the work, calls `complete_subtask`.
3. **JOIN** — Once all parallel subagents are done (poll with `get_plan_status`), collect all their outputs.
4. **MERGE** — You (the orchestrator) synthesise all outputs into the final coherent deliverable and call `finalize_plan`.

If subtasks have `depends_on`, fork a second wave once those dependencies complete.

Never run subtasks serially one-by-one. Always maximise parallelism.

## Your operating loop

```
create_plan(title, idea, subtasks=[...])   ← decompose now, all subtasks in one call
  → fork N subagents in parallel, each:
      get_next_task(plan_id)
      <do the work>
      complete_subtask(plan_id, subtask_id, output)
  → orchestrator polls get_plan_status until all done
  → orchestrator merges outputs → finalize_plan
```

## File editing rules

- Always `read` a file before editing it.
- Make minimal, targeted changes — never rewrite entire files.
- Preserve all existing HTML structure, CSS classes, scroll behaviour.
- If creating a website: use semantic HTML, `overflow: auto` on scrollable containers, no fixed heights on content sections.

## Current plans in {store_name}

{plan_summary}
"""),
                )],
            )

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
