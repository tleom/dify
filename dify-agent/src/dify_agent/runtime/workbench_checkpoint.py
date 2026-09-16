"""Persist native history before model requests and before tools can have effects."""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelRequest

from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState


@runtime_checkable
class HistoryCheckpointSink(Protocol):
    async def checkpoint_history(self, run_id: str, state: str) -> None: ...


@dataclass
class WorkbenchHistoryCheckpoint(AbstractCapability[None]):
    sink: HistoryCheckpointSink
    run_id: str
    seen_ids: set[str] | None = None
    _digest: str = ""
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def save(self, messages: Sequence[ModelMessage]) -> None:
        state = PydanticAIHistoryRuntimeState(
            messages=[
                replace(message, instructions=None) if isinstance(message, ModelRequest) else message
                for message in messages
            ]
        ).model_dump(mode="json")
        if self.seen_ids is not None:
            # Compaction may remove the original message metadata. Persist the
            # delivery cursor atomically with the history used after a crash.
            state["steering_delivered_ids"] = sorted(self.seen_ids)
        state = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(state.encode()).hexdigest()
        async with self._lock:
            if digest != self._digest:
                await self.sink.checkpoint_history(self.run_id, state)
                self._digest = digest

    async def before_model_request(self, ctx, request_context):
        await self.save(request_context.messages)
        return request_context

    async def before_tool_validate(self, ctx, *, call, tool_def, args):
        # A crash after an external write must retain the pending call, so the
        # successor can inspect its outcome instead of blindly issuing it again.
        await self.save(ctx.messages)
        return args

    async def after_run(self, ctx, *, result):
        await self.save(ctx.messages)
        return result
