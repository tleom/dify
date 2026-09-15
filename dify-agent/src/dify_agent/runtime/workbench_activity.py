"""Connect workbench activity state to the installed Pydantic AI lifecycle."""

import json
from dataclasses import dataclass, field

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ToolFailed
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
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext

from dify_agent.layers.workbench_activity import TOOL_NAME, WorkbenchActivityLayer
from dify_agent.protocol.schemas import WorkbenchActivityRunEvent, WorkbenchNarrativeData, WorkbenchProgressData
from dify_agent.runtime.event_sink import RunEventSink


@dataclass
class WorkbenchActivityCapability(AbstractCapability[None]):
    layer: WorkbenchActivityLayer
    sink: RunEventSink
    run_id: str
    invalid_calls: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.layer._native_run_id = self.run_id
        self.layer._publish = self.emit

    async def emit(self, data: WorkbenchProgressData) -> None:
        await self.sink.append_event(WorkbenchActivityRunEvent(run_id=self.run_id, data=data))

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
        self.invalid_calls.pop(call.tool_name, None)
        await self.layer.start_call(call, args)
        return args

    async def before_tool_validate(self, ctx, *, call, tool_def, args):
        if call.tool_name not in {"shell_run", "file_create", "file_edit"}:
            return args
        # Some compatible providers wrap the actual arguments in an extra JSON string.
        # Decode only complete JSON; never repair or execute a truncated script.
        try:
            value = json.loads(args) if isinstance(args, str) else args
            if isinstance(value, dict) and set(value) == {"arguments"}:
                nested = value["arguments"]
                decoded = json.loads(nested) if isinstance(nested, str) else nested
                if isinstance(decoded, dict):
                    return decoded
        except (TypeError, ValueError):
            pass
        return args

    async def after_tool_validate(self, ctx, *, call, tool_def, args):
        # External tools bypass before_tool_execute. The planned predecessor
        # resolves their activity even when the SDK collects them after functions.
        if tool_def.kind in {"external", "unapproved"}:
            await self.layer.start_call(call, args)
        return args

    async def on_tool_validate_error(self, ctx, *, call, tool_def, args, error):
        if call.tool_name in {"shell_run", "file_create", "file_edit"}:
            count = self.invalid_calls.get(call.tool_name, 0) + 1
            self.invalid_calls[call.tool_name] = count
            message = (
                "工具参数无效，本次操作未执行。请直接提供工具要求的 JSON 字段，不要嵌套 arguments。"
                "长脚本请分段使用 file_create / file_edit 保存，再通过 shell_run 执行简短命令。"
            )
            await self.layer.start_call(call, args)
            if count >= 3:
                await self.layer.finish_call(call.tool_call_id, call.tool_name, {"error": message}, failed=True)
                raise ValueError("连续三次工具参数无效，操作未执行。请缩短或拆分脚本后重试。") from error
            raise ToolFailed(message) from error
        if call.tool_name != TOOL_NAME:
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
