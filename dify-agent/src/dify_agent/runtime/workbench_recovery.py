"""Confirm executor death and clean conversation jobs before releasing an admission ticket."""
from __future__ import annotations

import json
from collections.abc import Mapping
from uuid import UUID

import httpx
from pydantic import BaseModel

from dify_agent.protocol.schemas import CreateRunRequest, RunStatus
from dify_agent.runtime_backend.local import _parse_local_binding_ref
from dify_agent.runtime_backend.profile import RuntimeBackendSettings
from dify_agent.storage.redis_run_store import RedisRunStore


async def manager(workspace: str, operation: str, payload: dict | None = None):
    settings = RuntimeBackendSettings()
    if not settings.workbench_manager_endpoint or not settings.workbench_manager_token:
        raise RuntimeError("Workbench recovery manager is not configured")
    async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
        response = await client.post(f"{settings.workbench_manager_endpoint.rstrip('/')}/sandboxes/{UUID(workspace)}/{operation}",
            headers={"Authorization": "Bearer " + settings.workbench_manager_token}, json=payload or {})
        response.raise_for_status()
        return response.json()


async def execution_owner(request: CreateRunRequest) -> dict[str, str] | None:
    for layer in request.composition.layers:
        if layer.type != "dify.runtime":
            continue
        config = layer.config.model_dump() if isinstance(layer.config, BaseModel) else layer.config
        if not isinstance(config, Mapping):
            continue
        reference = config.get("backend_binding_ref")
        if not isinstance(reference, str) or not reference.startswith("wb:"):
            continue
        binding, workspace = _parse_local_binding_ref(reference[3:])
        epoch = await manager(workspace, "runtime-epoch")
        if not epoch["running"]:
            raise RuntimeError("Workbench executor is not running")
        return {"binding_id": str(UUID(binding)), "workspace": str(UUID(workspace)), "epoch": epoch["epoch"]}
    return None


async def recover_fenced_run(store: RedisRunStore, run_id: str, status: RunStatus) -> RunStatus:
    raw = await store.redis.get(f"{store.prefix}:ticket:{run_id}")
    owner = json.loads(raw) if raw else None
    if not isinstance(owner, dict):
        return status
    operation = "fence-binding" if status == "running" else "stop-binding"
    result = await manager(owner["workspace"], operation, owner)
    if status == "running" and result["fenced"]:
        intent = await store.get_cancellation_intent(run_id)
        if intent is None:
            raise RuntimeError("Missing fenced run cancellation intent")
        status = (await store.finalize_cancellation(run_id, intent)).status
    return status
