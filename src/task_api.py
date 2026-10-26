"""Guarded task API. Credentials are resolved from the provider singleton only."""

from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from provider_api import ProviderRoute
from task_models import ModelOptionsInput, RunInput, FollowupInput, StrictInput, Provider, ModelID
import task_runner


router = APIRouter(prefix="/api/stores/{store_id}/task-runs", tags=["tasks"], route_class=ProviderRoute)


@router.get("")
async def list_runs(store_id: UUID, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    await task_runner.require_store(str(store_id))
    runs = await run_in_threadpool(task_runner.runner.store.list, str(store_id), offset, limit + 1)
    return {"runs": runs[:limit], "next_offset": offset + limit if len(runs) > limit else None}


class PriceInput(StrictInput):
    provider: Provider
    model: ModelID
    input_per_million: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    output_per_million: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)


@router.put("/pricing")
async def save_price(store_id: UUID, body: PriceInput):
    await task_runner.require_store(str(store_id))
    await run_in_threadpool(task_runner.runner.store.save_price, str(store_id), body.model_dump())
    return {"saved": True}


@router.get("/{run_id}/usage")
async def usage_details(store_id: UUID, run_id: UUID):
    await task_runner.require_store(str(store_id))
    return await run_in_threadpool(task_runner.runner.store.usage_details, str(store_id), str(run_id))


@router.post("", status_code=202)
async def create_run(store_id: UUID, body: RunInput):
    """Submit a fixed/profile provider or auto run; auto requires saved routing opt-in.

    Run details report lead_provider, worker_provider, routing_mode and bounded
    worker_candidates. Each planned task records its actual provider/model pair.
    """
    return await task_runner.runner.submit(str(store_id), body)


@router.post("/model-options")
async def model_options(store_id: UUID, body: ModelOptionsInput):
    """Discover models without inference.

    Auto adds provider-qualified candidates, lead_provider, worker_provider, mode
    and controlled warnings. Its models list belongs only to lead_provider; auto
    submissions omit model overrides to avoid ambiguous cross-provider IDs.
    """
    await task_runner.require_store(str(store_id))
    return await task_runner.runner.options(str(store_id), body.provider)


@router.get("/{run_id}")
async def get_run(store_id: UUID, run_id: UUID):
    await task_runner.require_store(str(store_id))
    return await run_in_threadpool(task_runner.runner.store.get, str(store_id), str(run_id))


@router.post("/{run_id}/cancel")
async def cancel_run(store_id: UUID, run_id: UUID, body: StrictInput):
    return await task_runner.runner.cancel(str(store_id), str(run_id))


@router.post("/{run_id}/messages", status_code=202)
async def continue_run(store_id: UUID, run_id: UUID, body: FollowupInput):
    """Append one idempotent follow-up to a finished task, retaining its ID and history."""
    return await task_runner.runner.submit(str(store_id), body, str(run_id))
