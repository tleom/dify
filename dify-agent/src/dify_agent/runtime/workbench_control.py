"""Refresh mode guidance and enforce planning's read-only tool boundary."""

from dataclasses import dataclass

from pydantic_ai import ModelRetry
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import InstructionPart
from pydantic_ai.tools import DeferredToolRequests

from dify_agent.layers.workbench_control import WorkbenchControlLayer

PLAN_REVIEW_RETRY = (
    "计划模式尚未提交可审阅的完整方案。方案已准备好时调用 exit_plan_mode；"
    "仍缺少关键决定时用 ask_human 讨论。不能用最终答复或任务清单跳过计划审阅。"
)


@dataclass
class WorkbenchControlCapability(AbstractCapability[None]):
    layer: WorkbenchControlLayer

    async def after_tool_validate(self, ctx, *, call, tool_def, args):
        # External/deferred tools never enter before_tool_execute in the SDK.
        # Validate every accepted call before it can leave the Agent process.
        return await self.before_tool_execute(ctx, call=call, tool_def=tool_def, args=args)

    def validate_response(self, response):
        if self.layer.runtime_state.state.plan.active and not response.tool_calls:
            raise ModelRetry(PLAN_REVIEW_RETRY)

    async def before_tool_execute(self, ctx, *, call, tool_def, args):
        # Metadata is assigned by the owning built-in layer, never inferred from
        # a tool's name, description or model-supplied arguments. Unknown/plugin
        # tools stay unavailable even if the model invents an undisclosed call.
        if self.layer.runtime_state.state.plan.active and not (tool_def.metadata or {}).get("workbench_plan"):
            raise ModelRetry(
                "当前仍在计划模式，本次工具未执行。请用 plan_inspect 调查和解析附件，"
                "通过 exit_plan_mode 提交完整方案；用户批准后才能实施或使用外部操作工具。"
            )
        return args

    async def before_model_request(self, ctx, request_context):
        await self.layer.request()
        await self.layer.resource_request()
        state = self.layer.runtime_state.state
        if state.plan.pending:
            await self.layer.request("apply_plan", {"revision": state.revision})
        # A review tool never unlocks sibling calls in the same model response.
        # Mode changes take effect at the next accepted model boundary.
        params = request_context.model_request_parameters
        if self.layer.runtime_state.state.plan.active:
            params.function_tools = [
                tool for tool in params.function_tools if (tool.metadata or {}).get("workbench_plan")
            ]
        else:
            params.function_tools = [
                tool for tool in params.function_tools if (tool.metadata or {}).get("workbench_plan") != "planning_only"
            ]
        params.instruction_parts = [
            *(params.instruction_parts or []),
            InstructionPart(content=self.layer.guidance(), dynamic=True),
        ]
        return request_context

    async def after_output_process(self, ctx, *, output_context, output):
        if (
            self.layer.runtime_state.state.plan.active
            and not isinstance(output, DeferredToolRequests)
            and not ctx.partial_output
        ):
            raise ModelRetry(PLAN_REVIEW_RETRY)
        return output
