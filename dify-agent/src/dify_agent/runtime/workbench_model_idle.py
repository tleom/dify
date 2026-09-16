"""Bound silent model waits without timing out tools or deferred human input."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import AgentStreamEvent, ModelResponse, PartDeltaEvent, PartStartEvent
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext

DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS = 180.0


@dataclass
class WorkbenchModelIdleCapability(AbstractCapability[None]):
    timeout_seconds: float = DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS
    _deadline: asyncio.Timeout | None = field(default=None, init=False)
    _waiting: bool = field(default=False, init=False)

    @asynccontextmanager
    async def guard(self) -> AsyncIterator[None]:
        deadline = asyncio.timeout(None)
        self._deadline = deadline
        try:
            async with deadline:
                yield
        except TimeoutError as exc:
            if not deadline.expired():
                raise
            raise UsageLimitExceeded(
                f"模型连续 {self.timeout_seconds:g} 秒未返回内容，已保留进度并交由后台恢复"
            ) from exc
        finally:
            self._waiting = False
            self._deadline = None

    def _touch(self) -> None:
        if self._waiting and self._deadline is not None and not self._deadline.expired():
            self._deadline.reschedule(asyncio.get_running_loop().time() + self.timeout_seconds)

    async def before_model_request(
        self, ctx: RunContext[None], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        self._waiting = True
        self._touch()
        return request_context

    async def after_model_request(
        self, ctx: RunContext[None], *, request_context: ModelRequestContext, response: ModelResponse
    ) -> ModelResponse:
        self._waiting = False
        if self._deadline is not None and not self._deadline.expired():
            self._deadline.reschedule(None)
        return response

    def observe(self, event: AgentStreamEvent) -> None:
        # These are actual model output events, including reasoning/tool arguments.
        # Transport keepalives never reach this hook and cannot mask a silent model.
        if isinstance(event, PartStartEvent | PartDeltaEvent):
            self._touch()
