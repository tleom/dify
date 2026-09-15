"""Connect workbench activity state to the installed Pydantic AI lifecycle."""

from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    ThinkingPart,
    ThinkingPartDelta,
    ToolReturnPart,
)
from pydantic_ai.models import InstructionPart, ModelRequestContext
from pydantic_ai.tools import RunContext

from dify_agent.layers.workbench_activity import TOOL_NAME, WorkbenchActivityLayer
from dify_agent.protocol.schemas import WorkbenchActivityRunEvent, WorkbenchNarrativeData, WorkbenchProgressData
from dify_agent.runtime.event_sink import RunEventSink


@dataclass
class WorkbenchActivityCapability(AbstractCapability[None]):
    layer: WorkbenchActivityLayer
    sink: RunEventSink
    run_id: str

    def __post_init__(self):
        self.layer._native_run_id = self.run_id
        self.layer._publish = self.emit

    async def emit(self, data: WorkbenchProgressData) -> None:
        await self.sink.append_event(WorkbenchActivityRunEvent(run_id=self.run_id, data=data))

    async def before_model_request(self, ctx, request_context):
        state = self.layer.runtime_state
        if (
            self.layer.config.enabled
            and state.needs_purpose
            and state.calls
            and state.reports_without_work < self.layer.config.max_reports_without_work
        ):
            # A missing report must not cancel/replay a business tool. Remind the
            # same model at its next boundary; the title is authored with context.
            params = request_context.model_request_parameters
            params.instruction_parts = [
                *(params.instruction_parts or []),
                InstructionPart(
                    content="The current tool group has no purpose title. Use report_activity(action='begin', title=...) "
                    "to briefly name the actual work you are doing, in the user's language. Supply a concrete purpose "
                    "rather than '执行任务'. Then continue the existing task without repeating completed operations.",
                    dynamic=True,
                ),
            ]
        return request_context

    async def after_model_request(
        self,
        ctx: RunContext[None],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        self.layer.plan_response(response)
        return response

    async def before_tool_execute(self, ctx, *, call, tool_def, args):
        await self.layer.start_call(call, args)
        return args

    async def after_tool_validate(self, ctx, *, call, tool_def, args):
        # External tools bypass before_tool_execute. The planned predecessor
        # resolves their activity even when the SDK collects them after functions.
        if tool_def.kind in {"external", "unapproved"}:
            await self.layer.start_call(call, args)
        return args

    async def on_tool_validate_error(self, ctx, *, call, tool_def, args, error):
        if call.tool_name != TOOL_NAME:
            await self.layer.start_call(call, args)
            raise error
        # A malformed progress report is a no-op tool result, not an Agent retry
        # that can spend the business task's retry budget or repeat side effects.
        return {"action": "invalid", "title": "", "goal": "", "activity_id": None, "evidence_call_ids": None}

    async def observe(self, event, *, run_step: int, text_delta: str | None = None) -> None:
        if isinstance(event, FunctionToolResultEvent):
            part = event.part
            await self.layer.finish_call(
                part.tool_call_id,
                part.tool_name or "unknown",
                part.content,
                failed=isinstance(part, RetryPromptPart)
                or (
                    isinstance(part, ToolReturnPart)
                    and (
                        part.outcome == "failed"
                        or (isinstance(part.metadata, dict) and part.metadata.get("is_error") is True)
                    )
                ),
            )
        if text_delta:
            self.layer.runtime_state.needs_purpose = True
            await self.emit(
                WorkbenchNarrativeData(
                    workbench_run_id=self.layer.config.workbench_run_id,
                    kind="text",
                    segment_id=f"{self.run_id}:{run_step}",
                    text=text_delta,
                )
            )
        thinking = None
        if isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart):
            thinking = event.part.content
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, ThinkingPartDelta):
            thinking = event.delta.content_delta
        if thinking:
            await self.emit(
                WorkbenchNarrativeData(
                    workbench_run_id=self.layer.config.workbench_run_id,
                    kind="reasoning",
                    segment_id=f"{self.run_id}:{run_step}:{event.index}",
                    text=thinking,
                )
            )
