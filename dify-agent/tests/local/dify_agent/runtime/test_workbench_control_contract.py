"""Stable mode transitions preserve revisions, paused goals and task truth."""

import pytest
from pydantic import ValidationError

from dify_agent.protocol.workbench_control import (
    TodoItem,
    PlanState,
    TodoWrite,
    WorkbenchControlState,
    change_goal,
    finish_goal,
    parse_command,
)


def test_goal_transition_fences_late_completion():
    first = change_goal(WorkbenchControlState(), parse_command("/goal 验证报告"))
    original = first.goal
    assert original is not None
    paused = change_goal(first, parse_command("/goal pause"))
    with pytest.raises(ValueError, match="目标已改变"):
        finish_goal(paused, goal_id=original.id, revision=original.revision, phase="complete", reason="完成")
    edited = change_goal(paused, parse_command("/goal edit 验证报告与附件"))
    assert edited.goal is not None and edited.goal.phase == "paused"
    resumed = change_goal(edited, parse_command("/goal resume"))
    assert resumed.goal is not None and resumed.goal.phase == "active"
    assert resumed.goal.id == original.id and resumed.goal.revision > original.revision


def test_complete_requires_finished_list_and_exact_goal():
    state = change_goal(WorkbenchControlState(), parse_command("/goal 整理材料"))
    state.todos = [TodoItem(content="核对附件", status="in_progress")]
    assert state.goal is not None
    args = dict(goal_id=state.goal.id, revision=state.goal.revision, phase="complete", reason="已验证")
    with pytest.raises(ValueError, match="任务清单"):
        finish_goal(state, **args)
    state.todos[0].status = "completed"
    result = finish_goal(state, **args)
    assert result.goal is not None and result.goal.phase == "complete"
    assert state.goal.phase == "active"


def test_commands_parse_exact_reserved_words():
    assert parse_command("/goal pause after checking").action == "create"
    assert parse_command("/goal").action == "status"
    assert parse_command("/plan off").action == "off"
    assert parse_command("/compact 保留数据口径").text == "保留数据口径"
    with pytest.raises(ValueError):
        parse_command("/goal edit")
    with pytest.raises(ValidationError):
        TodoWrite(todos=[TodoItem(content="a", status="in_progress"), TodoItem(content="b", status="in_progress")])


def test_goal_can_complete_without_creating_an_execution_list():
    state = change_goal(WorkbenchControlState(), parse_command("/goal 核对一个数值"))
    result = finish_goal(state, goal_id=state.goal.id, revision=1, phase="complete", reason="已核对")
    assert result.goal.phase == "complete" and result.todos == []


def test_plan_approval_is_bound_to_exact_content_run_and_version():
    state = PlanState(active=True)
    state.submit("# 第一版\n核对文件。", "run")
    state.answer(run_id="run", version=1, plan=state.review, approve=False)
    assert state.active and state.approved is None
    state.submit("# 第二版\n核对文件及来源。", "run")
    for kwargs in (
        dict(run_id="old-run", version=2, plan=state.review),
        dict(run_id="run", version=1, plan=state.review),
        dict(run_id="run", version=2, plan="# 不同内容"),
    ):
        with pytest.raises(ValueError, match="计划已改变"):
            state.answer(**kwargs, approve=True)
        assert state.active
    state.answer(run_id="run", version=2, plan=state.review, approve=True)
    restored = PlanState.model_validate_json(state.model_dump_json())
    assert not restored.active and restored.approved_version == 2
    assert restored.approved == "# 第二版\n核对文件及来源。"
