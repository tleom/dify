"""Refresh collaboration guidance at accepted model boundaries, outside history."""

from dataclasses import dataclass

from pydantic_ai import ModelRetry
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import InstructionPart

from dify_agent.layers.workbench_control import WorkbenchControlLayer


# Control and clarification tools remain available at a progress checkpoint.
CONTROL_TOOLS = frozenset(
    {
        "todo_write",
        "get_goal",
        "update_goal",
        "exit_plan_mode",
        "ask_human",
        "read_memory",
        "update_memory",
        "read_skill",
        "report_activity",
    }
)
TASK_CHECKPOINT_INTERVAL = 4


@dataclass
class WorkbenchControlCapability(AbstractCapability[None]):
    layer: WorkbenchControlLayer
    business_calls_since_todo: int = 0

    def task_checkpoint(self) -> str | None:
        state = self.layer.runtime_state.state
        mode_active = state.plan.active or (state.goal is not None and state.goal.phase == "active")
        if (mode_active or state.todos) and not any(item.status == "in_progress" for item in state.todos):
            return (
                "当前任务没有正在进行的清单步骤。请先用 todo_write 建立或更新完整清单，"
                "把即将执行的步骤设为 in_progress，再进行业务操作。仅把已验证的步骤标为 completed。"
            )
        if self.business_calls_since_todo >= TASK_CHECKPOINT_INTERVAL:
            return (
                "已连续执行多个业务工具，需要核对任务清单。请先用 todo_write 建立或更新当前完整清单："
                "将已验证完成的步骤立即标为 completed，下一步设为 in_progress；"
                "如果仍在处理同一步，可以如实保留 in_progress，不能为了继续而虚报完成。"
            )
        return None

    async def before_tool_execute(self, ctx, *, call, tool_def, args):
        if call.tool_name not in CONTROL_TOOLS and (checkpoint := self.task_checkpoint()):
            raise ModelRetry(checkpoint + " 本次业务工具尚未执行，更新清单后再调用。")
        return args

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        if call.tool_name == "todo_write":
            result = await handler(args)
            self.business_calls_since_todo = 0
            return result
        if call.tool_name in CONTROL_TOOLS:
            return await handler(args)
        try:
            return await handler(args)
        finally:
            self.business_calls_since_todo += 1

    async def before_model_request(self, ctx, request_context):
        await self.layer.request()
        await self.layer.resource_request()
        state = self.layer.runtime_state.state
        if state.plan.pending:
            await self.layer.request("apply_plan", {"revision": state.revision})
        # Use the SDK's dynamic instruction slot, not a synthetic history turn.
        params = request_context.model_request_parameters
        checkpoint = self.task_checkpoint()
        params.instruction_parts = [
            *(params.instruction_parts or []),
            InstructionPart(content=self.layer.guidance(), dynamic=True),
            *([InstructionPart(content="TASK CHECKPOINT: " + checkpoint, dynamic=True)] if checkpoint else []),
        ]
        return request_context
