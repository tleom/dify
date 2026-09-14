"""Enforce current-turn tool mentions while allowing preparation and suspension."""

from dataclasses import dataclass
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import ModelRetry
from pydantic_ai.messages import FunctionToolResultEvent, ToolReturnPart
from pydantic_ai.tools import DeferredToolRequests

from agenton.layers import LayerConfig, NoLayerDeps, PlainLayer


class RequiredToolGroup(BaseModel):
    name: str
    tool_names: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class WorkbenchMentionsConfig(LayerConfig):
    workbench_run_id: str = Field(min_length=1)
    tool_groups: list[RequiredToolGroup] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class WorkbenchMentionsState(BaseModel):
    workbench_run_id: str | None = None
    completed_tool_names: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


@dataclass
class WorkbenchMentionsLayer(PlainLayer[NoLayerDeps, WorkbenchMentionsConfig, WorkbenchMentionsState]):
    type_id: ClassVar[str | None] = "dify.workbench_mentions"
    config: WorkbenchMentionsConfig

    @classmethod
    def from_config(cls, config: WorkbenchMentionsConfig) -> Self:
        return cls(config=config)

    async def on_context_create(self) -> None:
        self.runtime_state = WorkbenchMentionsState(workbench_run_id=self.config.workbench_run_id)

    async def on_context_resume(self) -> None:
        if self.runtime_state.workbench_run_id != self.config.workbench_run_id:
            await self.on_context_create()

    @property
    def missing_groups(self) -> list[str]:
        completed = set(self.runtime_state.completed_tool_names)
        return [group.name for group in self.config.tool_groups if not completed.intersection(group.tool_names)]

    @property
    def prefix_prompts(self) -> list[str]:
        if not self.config.tool_groups:
            return []
        return [
            "本轮点名工具组须在最终答复前使用，每组调用一个适用工具即可："
            + "; ".join(f"{group.name} ({', '.join(group.tool_names)})" for group in self.config.tool_groups)
            + "。可先做准备、询问用户或请求环境更新。工具返回失败时如实说明，不能声称已取得结果。"
        ]

    def record_event(self, event: object) -> None:
        # Argument-validation retries and hallucinated calls are not execution.
        # A tool's explicit error observation still proves an access attempt.
        if isinstance(event, FunctionToolResultEvent) and isinstance(event.part, ToolReturnPart):
            name = event.part.tool_name
            allowed = {name for group in self.config.tool_groups for name in group.tool_names}
            if name in allowed and name not in self.runtime_state.completed_tool_names:
                self.runtime_state.completed_tool_names.append(name)

    def require_before_answer(self, agent) -> None:
        @agent.output_validator
        def validate(output):
            if isinstance(output, DeferredToolRequests):
                return output
            if self.missing_groups:
                raise ModelRetry("请先实际调用本轮点名工具再给出最终答复；尚未使用：" + "、".join(self.missing_groups))
            return output
