"""Give the model actionable tool observations before ending a workbench attempt.

Only consecutive failed business calls consume the budget. A successful call
resets it even when it is a different tool; progress reports do neither. The
service owns continuation of a failed run, so this capability never repeats a
tool invocation or repairs an incomplete command on the model's behalf.
"""

from dataclasses import dataclass

from pydantic import ValidationError
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    SkipToolExecution,
    ToolFailed,
    ToolFailedError,
    ToolRetryError,
)
from pydantic_ai.messages import FunctionToolResultEvent, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart

from dify_agent.layers.workbench_activity import TOOL_NAME, tool_result_failed

MAX_CONSECUTIVE_TOOL_FAILURES = 5


class WorkbenchToolFailureLimit(RuntimeError):
    """The current attempt used its consecutive business-tool failure budget."""


def validation_feedback(error: ValidationError | ModelRetry) -> str:
    if isinstance(error, ValidationError):
        items = error.errors(include_input=False, include_url=False, include_context=False)
        details = "; ".join(f"{'.'.join(map(str, item['loc'])) or 'arguments'}: {item['msg']}" for item in items[:8])
    else:
        details = str(error)
    return details[:2000]


@dataclass
class WorkbenchToolRecoveryCapability(AbstractCapability[None]):
    consecutive_failures: int = 0
    exhausted: bool = False

    def check_budget(self) -> None:
        if self.exhausted:
            raise WorkbenchToolFailureLimit("工具连续调用失败 5 次，本轮任务失败。已保留进度，可继续任务。")

    async def before_model_request(self, ctx, request_context):
        # Drain and capture the failed tool result before ending the attempt.
        self.check_budget()
        return request_context

    async def before_tool_validate(self, ctx, *, call, tool_def, args):
        if tool_def.kind != "external" or ctx.tool_manager is None:
            return args
        response = next((message for message in reversed(ctx.messages) if isinstance(message, ModelResponse)), None)
        deferred = (
            []
            if response is None
            else [
                part
                for part in response.parts
                if isinstance(part, ToolCallPart)
                and (definition := ctx.tool_manager.get_tool_def(part.tool_name)) is not None
                and definition.kind == "external"
            ]
        )
        if len(deferred) > 1:
            raise ModelRetry("同一轮只能提交一个补充信息或环境更新请求；请合并问题字段，修正后只调用一个外部工具。")
        return args

    async def after_run(self, ctx, *, result):
        self.check_budget()
        return result

    async def on_tool_validate_error(self, ctx, *, call, tool_def, args, error):
        if call.tool_name == TOOL_NAME:
            # An invalid progress report must not derail business work.
            return {"action": "invalid", "title": "", "goal": "", "activity_id": None, "evidence_call_ids": None}
        hint = "请按工具 schema 修正这些字段后重新调用，不要猜测用户答案。"
        if call.tool_name in {"shell_run", "file_create", "file_edit"}:
            hint = (
                "请直接传入工具要求的 JSON 字段，不要嵌套 arguments。"
                "长脚本请分段使用 file_create / file_edit 保存，再用 shell_run 执行简短命令。"
                "不完整的 JSON 或脚本不会被执行。"
            )
        raise ToolFailed(f"工具参数无效，本次操作未执行。{validation_feedback(error)}。{hint}") from error

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        try:
            return await handler(args)
        except (ApprovalRequired, CallDeferred, SkipToolExecution, ToolFailed, ToolFailedError):
            raise
        except ToolRetryError as error:
            # Turn retries into observations so the SDK's per-tool counter does
            # not terminate a task whose intervening business calls succeeded.
            raise ToolFailed(str(error.tool_retry.content)[:2000]) from error
        except Exception as error:
            raise ToolFailed(
                f"工具执行失败：{str(error)[:2000]}。请根据错误和已有产物调整下一步；"
                "涉及外部写入或提交时先核实结果，不要重复执行结果不明的操作。"
            ) from error

    async def before_tool_execute(self, ctx, *, call, tool_def, args):
        if self.exhausted and call.tool_name != TOOL_NAME:
            # Return a settled, explicitly unexecuted call instead of raising
            # out of the SDK's batch and losing earlier completed observations.
            raise ToolFailedError(
                ToolReturnPart(
                    tool_name=call.tool_name,
                    tool_call_id=call.tool_call_id,
                    content="本轮工具已连续失败 5 次，本次操作未执行。请保留已有结果并在继续任务时重新判断下一步。",
                    outcome="failed",
                    metadata={"workbench_budget_skipped": True},
                )
            )
        return args

    def observe(self, event) -> None:
        if not isinstance(event, FunctionToolResultEvent):
            return
        part = event.part
        if part.tool_name == TOOL_NAME:
            return
        if (
            isinstance(part, ToolReturnPart)
            and isinstance(part.metadata, dict)
            and part.metadata.get("workbench_budget_skipped")
        ):
            return
        failed = isinstance(part, RetryPromptPart) or (
            isinstance(part, ToolReturnPart)
            and (
                part.outcome == "failed" or (isinstance(part.metadata, dict) and part.metadata.get("is_error") is True)
            )
        )
        if tool_result_failed(part.tool_name or "unknown", part.content, failed=failed):
            self.consecutive_failures += 1
        else:
            self.consecutive_failures = 0
        if self.consecutive_failures >= MAX_CONSECUTIVE_TOOL_FAILURES:
            self.exhausted = True
