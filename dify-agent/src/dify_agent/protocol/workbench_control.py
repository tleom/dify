"""Serializable collaboration state shared by the workbench API and Agent.

Goal lifecycle, planning and the standing task list are control state, not
conversation text. They survive a history summary and never derive authority
from a model's final answer. Inspired by DeepSeek Harness's goal/plan/todo
contracts; persistence and execution fencing belong to the owning API.
"""

from __future__ import annotations

from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoalState(ControlModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    revision: int = Field(default=1, ge=1)
    objective: str = Field(min_length=1, max_length=20000)
    phase: Literal["active", "paused", "blocked", "complete"] = "active"
    rounds_started: int = Field(default=0, ge=0)
    max_rounds: int | None = Field(default=None, ge=1, le=10000)
    reason: str | None = Field(default=None, max_length=4000)
    last_run_id: str | None = None
    resource_mentions: dict[str, list[str]] = Field(default_factory=dict)
    started_at: float | None = Field(default=None, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    active_since: float | None = Field(default=None, ge=0)


class TodoItem(ControlModel):
    content: str = Field(min_length=1, max_length=2000)
    status: Literal["pending", "in_progress", "completed"]


class TodoWrite(ControlModel):
    todos: list[TodoItem] = Field(max_length=100)

    @model_validator(mode="after")
    def one_current_task(self):
        if sum(item.status == "in_progress" for item in self.todos) > 1:
            raise ValueError("同时只能有一个正在进行的步骤")
        if any(not item.content.strip() for item in self.todos):
            raise ValueError("任务内容不能为空")
        return self


class GoalUpdate(ControlModel):
    goal_id: str
    revision: int = Field(ge=1)
    phase: Literal["complete", "blocked"]
    reason: str = Field(min_length=1, max_length=4000)


class PlanState(ControlModel):
    active: bool = False
    pending: bool = False
    review: str | None = Field(default=None, max_length=100000)
    review_run_id: str | None = None


class CompactionState(ControlModel):
    id: str
    phase: Literal["queued", "compacting", "compacted", "unchanged", "failed"]
    before_tokens: int | None = None
    after_tokens: int | None = None
    message: str | None = None


class WorkbenchControlState(ControlModel):
    revision: int = Field(default=0, ge=0)
    goal: GoalState | None = None
    plan: PlanState = Field(default_factory=PlanState)
    todos: list[TodoItem] = Field(default_factory=list)
    todos_run_id: str | None = None
    compaction: CompactionState | None = None


class SlashCommand(ControlModel):
    name: Literal["goal", "plan", "compact"]
    action: Literal["status", "create", "edit", "pause", "resume", "clear", "on", "off", "compact"]
    text: str = ""


def parse_command(value: str) -> SlashCommand:
    """Parse exact control words; e.g. 'pause after checking' is an objective."""
    parts = value.strip().split(maxsplit=1)
    word, suffix = (parts[0], parts[1].strip() if len(parts) > 1 else "") if parts else ("", "")
    if word == "/goal":
        if not suffix:
            return SlashCommand(name="goal", action="status")
        if suffix in {"pause", "resume", "clear"}:
            return SlashCommand(name="goal", action=cast(Literal["pause", "resume", "clear"], suffix))
        if suffix == "edit":
            raise ValueError("用法：/goal edit 新的目标")
        if suffix.startswith("edit "):
            text = suffix[5:].strip()
            if not text:
                raise ValueError("目标不能为空")
            return SlashCommand(name="goal", action="edit", text=text)
        return SlashCommand(name="goal", action="create", text=suffix)
    if word == "/plan":
        return SlashCommand(
            name="plan", action="off" if suffix == "off" else "on", text="" if suffix == "off" else suffix
        )
    if word == "/compact":
        return SlashCommand(name="compact", action="compact", text=suffix)
    raise ValueError("未知命令，可使用 /goal、/plan 或 /compact")


def change_goal(state: WorkbenchControlState, command: SlashCommand) -> WorkbenchControlState:
    """Apply a human goal command under the caller's conversation row lock."""
    result = state.model_copy(deep=True)
    goal = result.goal
    if command.action == "status":
        return result
    if command.action == "create":
        if goal is not None and goal.phase != "complete":
            raise ValueError("当前目标尚未完成，请用 /goal edit 修改，或用 /goal clear 清除")
        result.goal = GoalState(objective=command.text)
        result.todos, result.todos_run_id = [], None
    elif command.action == "clear":
        result.goal = None
    else:
        if goal is None:
            raise ValueError("当前没有目标，请使用 /goal 目标内容")
        if command.action == "edit":
            goal.objective = GoalState(objective=command.text).objective
        elif command.action == "pause":
            if goal.phase != "active":
                raise ValueError("只有运行中的目标可以暂停")
            goal.phase = "paused"
        elif command.action == "resume":
            if goal.phase == "complete":
                raise ValueError("目标已完成，请创建新的目标")
            if goal.max_rounds is not None and goal.rounds_started >= goal.max_rounds:
                raise ValueError("目标已达到自动执行轮次上限，请创建新目标")
            goal.phase, goal.reason = "active", None
        else:
            raise ValueError("目标命令无效")
        goal.revision += 1
    result.revision += 1
    return result


def finish_goal(
    state: WorkbenchControlState, *, goal_id: str, revision: int, phase: Literal["complete", "blocked"], reason: str
) -> WorkbenchControlState:
    """Fence a model's result against a human edit, pause, clear or new goal."""
    goal = state.goal
    if goal is None or goal.id != goal_id or goal.revision != revision or goal.phase != "active":
        raise ValueError("目标已改变，请读取当前目标后再更新")
    if not reason.strip() or len(reason) > 4000:
        raise ValueError("请提供已验证的完成结果或具体阻塞原因")
    if phase == "complete" and any(item.status != "completed" for item in state.todos):
        raise ValueError("任务清单仍有未完成步骤，请完成工作并更新清单")
    result = state.model_copy(deep=True)
    assert result.goal is not None
    result.goal.phase, result.goal.reason = phase, reason.strip()
    result.goal.revision += 1
    result.revision += 1
    return result
