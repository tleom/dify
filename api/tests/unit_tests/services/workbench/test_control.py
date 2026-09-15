"""Mode commands use real SQL transactions and the existing run admission path."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from werkzeug.exceptions import BadRequest, Conflict, Forbidden

from models.workbench import WorkbenchRun
from services.workbench import control, service
from tests.unit_tests.services.workbench.test_followups import queue_fixture

queue = pytest.fixture(queue_fixture)


def command(queue: SimpleNamespace, text: str, key: str | None = None) -> dict[str, Any]:
    return control.issue(queue.owner[0], queue.owner[1], queue.chat_id, command=text, request_key=key or str(uuid4()))


def test_goal_command_idempotency_and_pause_prevent_duplicate_runs(queue: SimpleNamespace) -> None:
    first = command(queue, "/goal 校验文件", "same")
    again = command(queue, "/goal 校验文件", "same")
    assert first["state"]["goal"]["id"] == again["state"]["goal"]["id"]
    assert again["state"]["goal"]["rounds_started"] == 1
    assert control.drive_goal(queue.owner[0], queue.owner[1], queue.chat_id) is None
    command(queue, "/goal pause")
    run_id = first["state"]["goal"]["last_run_id"]
    queue.finish(run_id)
    assert control.drive_goal(queue.owner[0], queue.owner[1], queue.chat_id) is None
    with pytest.raises(Conflict):
        command(queue, "/goal another", "same")


def test_goal_waits_for_user_messages_and_resets_todos_only_at_admission(queue: SimpleNamespace) -> None:
    run = queue.send("原消息")
    queue.running(run["id"])
    ticket = queue.get(run["id"]).backend_run_id
    payload = control.AgentControlPayload(
        tenant_id=queue.owner[0],
        account_id=queue.owner[1],
        app_id=queue.app_id,
        workbench_run_id=run["id"],
        backend_run_id=ticket,
        action="todo_write",
        request_key="todo",
        data={"todos": [{"content": "当前步骤", "status": "in_progress"}]},
    )
    control.agent_control(payload)
    queued = queue.send("下一条消息")
    assert control.read(queue.owner[0], queue.owner[1], queue.chat_id)["todos"][0]["content"] == "当前步骤"
    queue.finish(run["id"])
    assert queue.advance() == queued["id"]
    assert control.read(queue.owner[0], queue.owner[1], queue.chat_id)["todos"] == []
    with pytest.raises(Forbidden):
        control.agent_control(payload)


def test_plan_review_requires_explicit_action_and_disables_goal_driver(queue: SimpleNamespace) -> None:
    command(queue, "/plan")
    goal = command(queue, "/goal 输出报告")
    assert goal["state"]["goal"]["rounds_started"] == 0
    run = queue.send("请制定计划")
    with queue.factory.begin() as session:
        chat = service._chat(session, queue.owner[0], queue.owner[1], queue.chat_id, lock=True)
        row = session.get(WorkbenchRun, run["id"])
        state = control.load(session, chat)
        state.plan.review, state.plan.review_run_id = "# 实施计划\n\n检查后生成。", row.id
        control.save(session, chat, state)
        payload = json.loads(row.payload)
        payload["pending"] = {
            "tool_name": "exit_plan_mode",
            "tool_call_id": "plan-call",
            "args": {
                "title": "计划",
                "question": "选择下一步",
                "markdown": "# 实施计划",
                "fields": [{"name": "feedback", "label": "意见", "type": "paragraph", "required": False}],
                "actions": [{"id": "approve", "label": "开始执行"}, {"id": "keep_planning", "label": "继续规划"}],
            },
        }
        row.payload, row.status = json.dumps(payload), "waiting_input"
    with pytest.raises(BadRequest):
        service.resume(queue.owner[0], queue.owner[1], run["id"], {}, None)
    service.resume(queue.owner[0], queue.owner[1], run["id"], {}, "approve")
    assert control.read(queue.owner[0], queue.owner[1], queue.chat_id)["plan"]["active"] is False


def test_exhausted_failure_blocks_same_goal_generation(queue: SimpleNamespace) -> None:
    result = command(queue, "/goal 核验")
    queue.finish(result["state"]["goal"]["last_run_id"], "failed")
    control.settle(queue.owner[0], queue.owner[1], queue.chat_id)
    assert control.read(queue.owner[0], queue.owner[1], queue.chat_id)["goal"]["phase"] == "blocked"
    command(queue, "/goal resume")
    assert control.read(queue.owner[0], queue.owner[1], queue.chat_id)["goal"]["rounds_started"] == 2
