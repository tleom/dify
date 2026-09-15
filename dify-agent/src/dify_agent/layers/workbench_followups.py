"""Snapshot-backed delivery cursor for supplements to the current workbench task."""

import asyncio
import logging
from dataclasses import dataclass
from typing import ClassVar, Literal

import httpx
from pydantic import BaseModel, Field

from agenton.layers import LayerConfig, LayerDeps, PlainLayer
from dify_agent.layers.execution_context.layer import DifyExecutionContextLayer

logger = logging.getLogger(__name__)


class WorkbenchFollowupsDeps(LayerDeps):
    execution_context: DifyExecutionContextLayer


class WorkbenchFollowupsState(BaseModel):
    workbench_run_id: str | None = None
    seen_ids: set[str] = Field(default_factory=set)


class FollowupMessage(BaseModel):
    id: str
    content: str


class FollowupBatch(BaseModel):
    messages: list[FollowupMessage]
    sealed: bool


@dataclass
class WorkbenchFollowupsLayer(PlainLayer[WorkbenchFollowupsDeps, LayerConfig, WorkbenchFollowupsState]):
    type_id: ClassVar[str | None] = "dify.workbench_followups"
    config: LayerConfig
    inner_api_url: str
    inner_api_key: str

    async def on_context_create(self) -> None:
        self.runtime_state = WorkbenchFollowupsState(
            workbench_run_id=self.deps.execution_context.config.workbench_run_id
        )

    async def on_context_resume(self) -> None:
        if self.runtime_state.workbench_run_id != self.deps.execution_context.config.workbench_run_id:
            await self.on_context_create()

    async def poll(self, http_client: httpx.AsyncClient, run_id: str, *, action: Literal["poll", "seal"] = "poll"):
        # Poll and seal are idempotent. A transient control-plane failure must
        # neither abort finished tool work nor let End skip accepted input.
        # Cancellation interrupts both the HTTP request and the bounded backoff.
        delay = 0.25
        while True:
            try:
                return await self._request(http_client, run_id, action=action)
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in (408, 429) and error.response.status_code < 500:
                    raise
            except httpx.TransportError:
                pass
            logger.warning("Waiting to reconnect workbench follow-ups: run=%s action=%s", run_id, action)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5)

    async def _request(self, http_client: httpx.AsyncClient, run_id: str, *, action: Literal["poll", "seal"]):
        context = self.deps.execution_context.config
        response = await http_client.post(
            self.inner_api_url.rstrip("/") + "/inner/api/agent/workbench/followups",
            headers={"X-Inner-Api-Key": self.inner_api_key},
            json={
                "tenant_id": context.tenant_id,
                "account_id": context.user_id,
                "app_id": context.app_id,
                "workbench_run_id": context.workbench_run_id,
                "backend_run_id": run_id,
                "seen_ids": sorted(self.runtime_state.seen_ids),
                "action": action,
            },
            timeout=15,
        )
        response.raise_for_status()
        return FollowupBatch.model_validate(response.json())
