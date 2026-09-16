"""Observe the native compactor without changing its history or budget policy."""

from dataclasses import dataclass
from uuid import uuid4

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext
from pydantic_ai_harness.compaction import ContextUsage, ReportContextUsage, TieredCompaction, estimate_context_tokens

from dify_agent.protocol.schemas import ContextStatusData, ContextStatusRunEvent
from dify_agent.runtime.event_sink import RunEventSink


@dataclass
class WorkbenchContextStatus(AbstractCapability[None]):
    compaction: TieredCompaction[None] | None
    window_tokens: int | None
    sink: RunEventSink
    run_id: str

    async def emit(self, **values) -> None:
        await self.sink.append_event(
            ContextStatusRunEvent(
                run_id=self.run_id,
                data=ContextStatusData(window_tokens=self.window_tokens, **values),
            )
        )

    async def before_model_request(
        self,
        ctx: RunContext[None],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        async def used_tokens(context: ModelRequestContext) -> int:
            if self.window_tokens is None:
                return estimate_context_tokens(
                    context.messages,
                    model_request_parameters=context.model_request_parameters,
                )
            readings: list[ContextUsage] = []
            reporter = ReportContextUsage(on_usage=readings.append, context_window=self.window_tokens)
            await reporter.before_model_request(ctx, context)
            return readings[-1].used_tokens

        before = await used_tokens(request_context)
        await self.emit(phase="usage", used_tokens=before)
        if self.compaction is None or before <= (self.compaction.target_tokens or 0):
            return request_context

        identifier = str(uuid4())
        await self.emit(phase="compacting", used_tokens=before, before_tokens=before, compaction_id=identifier)
        try:
            result = await self.compaction.before_model_request(ctx, request_context)
        except Exception:
            await self.emit(phase="failed", used_tokens=before, before_tokens=before, compaction_id=identifier)
            raise
        await self.emit(
            phase="compacted", used_tokens=await used_tokens(result), before_tokens=before, compaction_id=identifier
        )
        return result

    async def after_model_request(
        self,
        ctx: RunContext[None],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        # Each response's usage is the current provider request, unlike the run's
        # accumulated billing counters. Leave a missing provider reading estimated.
        if response.usage.input_tokens > 0:
            await self.emit(
                phase="usage", used_tokens=response.usage.input_tokens + response.usage.output_tokens, estimated=False
            )
        return response
