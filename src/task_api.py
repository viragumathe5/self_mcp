"""Guarded task API. Credentials are resolved from the provider singleton only."""

from uuid import UUID

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from provider_api import ProviderRoute
from task_models import ModelOptionsInput, RunInput, StrictInput
import task_runner


router = APIRouter(prefix="/api/stores/{store_id}/task-runs", tags=["tasks"], route_class=ProviderRoute)


@router.get("")
async def list_runs(store_id: UUID):
    await task_runner.require_store(str(store_id))
    return {"runs": await run_in_threadpool(task_runner.runner.store.list, str(store_id))}


@router.post("", status_code=202)
async def create_run(store_id: UUID, body: RunInput):
    return await task_runner.runner.submit(str(store_id), body)


@router.post("/model-options")
async def model_options(store_id: UUID, body: ModelOptionsInput):
    await task_runner.require_store(str(store_id))
    return await task_runner.runner.options(str(store_id), body.provider)


@router.get("/{run_id}")
async def get_run(store_id: UUID, run_id: UUID):
    await task_runner.require_store(str(store_id))
    return await run_in_threadpool(task_runner.runner.store.get, str(store_id), str(run_id))


@router.post("/{run_id}/cancel")
async def cancel_run(store_id: UUID, run_id: UUID, body: StrictInput):
    return await task_runner.runner.cancel(str(store_id), str(run_id))
