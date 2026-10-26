"""MCP gateway to the API-owned runner; never owns jobs or provider credentials."""
import json
from uuid import UUID

import httpx
from mcp.types import Tool, TextContent, CallToolResult
from pydantic import ValidationError

from task_models import RunInput, FollowupInput, ModelOptionsInput, StrictInput


class RunReference(StrictInput):
    run_id: str


class FollowupReference(FollowupInput):
    run_id: str


def task_tools():
    return [
        Tool(name="start_task_run", description="Start a NEW task only. For follow-ups, edits, or retries in an existing task, use continue_task_run to retain its history. mode=delegate runs a lead/worker team; mode=direct asks one model (lead_model selects it). Hosted calls may incur charges. Use only when the user requests execution. Generates text/code; does not execute code. Returns a run ID; poll get_task_run.", inputSchema=RunInput.model_json_schema()),
        Tool(name="continue_task_run", description="Continue the SAME task with a follow-up in goal. Earlier requests and saved results are included automatically. Get the current run first; use its turn_id (or id for legacy runs) as expected_turn_id and copy its provider/mode/settings unless the user changes them. Generate one UUID message_id and reuse it for retries of this exact message to prevent duplicate work. The run must have finished or been cancelled. Returns the same run ID. Hosted calls may incur charges.", inputSchema=FollowupReference.model_json_schema()),
        Tool(name="list_task_runs", description="List recent supervised task runs in this project.", inputSchema=StrictInput.model_json_schema()),
        Tool(name="get_task_run", description="Read a supervised run's progress, worker outputs, errors and final answer.", inputSchema=RunReference.model_json_schema()),
        Tool(name="cancel_task_run", description="Stop a supervised task run. Already-issued calls may still be billed.", inputSchema=RunReference.model_json_schema()),
        Tool(name="task_model_options", description="Discover models for a configured provider or auto routing without generating text.", inputSchema=ModelOptionsInput.model_json_schema()),
        Tool(name="task_providers", description="Read configured provider IDs and routing preferences; no credentials are returned.", inputSchema=StrictInput.model_json_schema()),
    ]


NAMES = frozenset(tool.name for tool in task_tools())


async def call_task_tool(store_id, name, arguments):
    try:
        root = f"/api/stores/{UUID(store_id)}/task-runs"
        method, path, body = "GET", root, None
        if name == "start_task_run":
            body = RunInput.model_validate(arguments).model_dump()
            method = "POST"
        elif name == "continue_task_run":
            followup = FollowupReference.model_validate(arguments)
            body = followup.model_dump(exclude={"run_id"})
            method, path = "POST", root + "/" + str(UUID(followup.run_id)) + "/messages"
        elif name == "task_model_options":
            body = ModelOptionsInput.model_validate(arguments).model_dump()
            method, path = "POST", root + "/model-options"
        elif name in ("get_task_run", "cancel_task_run"):
            ref = RunReference.model_validate(arguments)
            path = root + "/" + str(UUID(ref.run_id))
            if name == "cancel_task_run":
                method, path, body = "POST", path + "/cancel", {}
        elif name in ("list_task_runs", "task_providers"):
            StrictInput.model_validate(arguments)
            if name == "task_providers":
                path = "/api/providers"
        else:
            raise ValueError
    except (ValueError, ValidationError):
        return failure("Invalid task arguments. Use the tool's schema and valid UUID run IDs.")
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=45,
                                     follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, path, json=body,
                                            headers={"Content-Type": "application/json"})
        data = response.json()
        if response.status_code not in (200, 202):
            # API errors are sanitized by ProviderRoute; never reflect non-string validation bodies.
            detail = data.get("detail") if isinstance(data, dict) else None
            return failure(detail if isinstance(detail, str) else "Task API rejected the request.")
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))])
    except httpx.TimeoutException:
        return failure("Task API timed out. Submission may have been accepted: list_task_runs before starting again.")
    except (httpx.RequestError, ValueError):
        return failure("Task API is unavailable or returned an invalid response. Ensure the API is running on port 8000. Check list_task_runs before resubmitting.")


def failure(message):
    return CallToolResult(isError=True, content=[TextContent(type="text", text=message)])
