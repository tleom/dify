"""Regression coverage for planning admission and initial goal input recovery."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from dify_agent.protocol.schemas import DeferredToolCallPayload
from werkzeug.exceptions import Conflict

from models.workbench import WorkbenchChat, WorkbenchRevision, WorkbenchRun
from services.workbench import control, followups, service
from tests.unit_tests.services.workbench.test_followups import queue_fixture

queue = pytest.fixture(queue_fixture)


def test_api_rejects_environment_update_while_plan_is_unapproved(queue, monkeypatch):
    from extensions.ext_redis import redis_client
    from models.agent import AgentConfigVersionKind, AgentWorkspaceBinding
    from services.workbench import runtime

    result = control.issue(*queue.owner, queue.chat_id, command="/plan 调查附件", request_key="plan")
    run_id = result["run"]["id"]
    queue.running(run_id)
    conversation, binding = str(uuid4()), str(uuid4())
    with queue.factory.begin() as session:
        chat = session.get(WorkbenchChat, queue.chat_id)
        chat.conversation_id = conversation
        session.add(
            AgentWorkspaceBinding(
                id=binding,
                tenant_id=chat.tenant_id,
                app_id=chat.app_id,
                workspace_id=str(uuid4()),
                agent_id=chat.agent_id,
                agent_config_version_id=str(uuid4()),
                agent_config_version_kind=AgentConfigVersionKind.SNAPSHOT,
                backend_binding_ref="binding",
            )
        )
    redis = MagicMock()
    monkeypatch.setattr(redis_client, "_client", redis)
    terminal = SimpleNamespace(
        deferred_tool_call=DeferredToolCallPayload(
            tool_call_id="install",
            tool_name="update_shared_environment",
            args={"python": ["pandas"], "reason": "prepare"},
        ),
        session_snapshot=SimpleNamespace(model_dump_json=lambda: '{"layers":[]}'),
    )
    with pytest.raises(Conflict, match="计划尚未批准"):
        runtime.pause(queue.owner[0], conversation, queue.owner[1], terminal, binding)
    assert queue.get(run_id).status == "running"
    assert "pending" not in json.loads(queue.get(run_id).payload)
    redis.set.assert_not_called()
    redis.xadd.assert_not_called()


@pytest.mark.parametrize("mode_change", ["before_dispatch", "during_startup", "none"])
def test_environment_dispatch_rechecks_plan_without_installing(queue, monkeypatch, mode_change):
    from extensions.ext_redis import redis_client
    from services.workbench import files
    from tasks import workbench_tasks as tasks

    run_id = queue.send("更新依赖")["id"]
    with queue.factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        data = json.loads(run.payload)
        data["pending"] = {
            "tool_name": "update_shared_environment",
            "tool_call_id": "install",
            "args": {"python": ["pandas"]},
        }
        run.payload, run.status = json.dumps(data), "environment_update"
    redis = MagicMock()
    redis.zcard.return_value = 0
    redis.get.return_value = None
    monkeypatch.setattr(redis_client, "_client", redis)
    monkeypatch.setattr(tasks, "authorize", lambda *_: None)
    monkeypatch.setattr(tasks, "event", MagicMock())
    monkeypatch.setattr(tasks.dispatch, "delay", MagicMock())
    monkeypatch.setattr(tasks.update_environment, "apply_async", MagicMock())
    install = MagicMock(return_value={"status": "ready"})
    monkeypatch.setattr(files, "manager", install)

    def activate_plan():
        control.issue(*queue.owner, queue.chat_id, command="/plan", request_key="enable-plan")

    def ensure(*_):
        if mode_change == "during_startup":
            activate_plan()
        return "workspace"

    monkeypatch.setattr(files, "ensure_workspace", ensure)
    if mode_change == "before_dispatch":
        activate_plan()
    tasks.update_environment.run(*queue.owner)
    row = queue.get(run_id)
    assert row.status == "queued"
    result = json.loads(row.payload)["continuation"]["calls"]["install"]
    if mode_change == "none":
        assert install.call_count == 1
        assert install.call_args.args[1] == "environment"
        assert result["status"] == "ready"
    else:
        install.assert_not_called()
        assert result["status"] == "failed"
        assert "计划尚未批准" in result["error"]
    assert run_id in queue.published


@pytest.mark.parametrize("retry", ["driver", "same_command"])
def test_first_goal_recovery_uses_current_configuration_once(queue, monkeypatch, retry):
    with monkeypatch.context() as patch:
        patch.setattr(service, "enqueue", MagicMock(side_effect=RuntimeError("enqueue gap")))
        with pytest.raises(RuntimeError):
            control.issue(*queue.owner, queue.chat_id, command="/goal 原始要求", request_key="original")
    with queue.factory.begin() as session:
        chat = session.get(WorkbenchChat, queue.chat_id)
        chat.version = 2
        original = session.query(WorkbenchRevision).filter_by(chat_id=queue.chat_id, version=1).one()
        session.add(
            WorkbenchRevision(
                id=str(uuid4()),
                chat_id=chat.id,
                tenant_id=chat.tenant_id,
                account_id=chat.account_id,
                version=2,
                selection=original.selection,
                effective_soul=original.effective_soul,
            )
        )
    current = service.read_chat(*queue.owner, queue.chat_id)
    monkeypatch.setattr(service, "read_chat", lambda *_: {**current, "version": 2})
    if retry == "driver":
        admitted = control.drive_goal(*queue.owner, queue.chat_id)
    else:
        admitted = control.issue(*queue.owner, queue.chat_id, command="/goal 原始要求", request_key="original")["run"]
    assert admitted["query"] == "原始要求"
    assert control.drive_goal(*queue.owner, queue.chat_id) is None
    state = control.read(*queue.owner, queue.chat_id)
    assert state["goal"]["rounds_started"] == 1
    assert queue.published == [admitted["id"]]
    with queue.factory() as session:
        revision = session.get(WorkbenchRevision, queue.get(admitted["id"]).revision_id)
        assert revision.version == 2


@pytest.mark.parametrize("action", ["remove", "steer"])
def test_queued_goal_move_reconciles_state_without_replaying_command(queue, monkeypatch, action):
    busy = queue.send("正在执行")
    goal = control.issue(*queue.owner, queue.chat_id, command="/goal 原始目标", request_key="original")
    if action == "remove":
        assert followups.remove(*queue.owner, goal["run"]["id"])["status"] == "discarded"
    else:
        assert followups.steer(*queue.owner, goal["run"]["id"], busy["id"])["status"] == "steered"
    queue.finish(busy["id"])
    forbidden_replay = MagicMock(side_effect=AssertionError("must not recursively replay commands"))
    monkeypatch.setattr(control, "issue", forbidden_replay)
    continuation = control.drive_goal(*queue.owner, queue.chat_id)
    assert control.drive_goal(*queue.owner, queue.chat_id) is None
    state = control.read(*queue.owner, queue.chat_id)["goal"]
    forbidden_replay.assert_not_called()
    if action == "remove":
        assert continuation is None
        assert state["phase"] == "paused"
        assert state["rounds_started"] == 0
        assert state["source_request_key"] is None
        assert queue.published == [busy["id"]]
    else:
        assert continuation["is_continuation"] is True
        assert state["rounds_started"] == 2
        assert json.loads(queue.get(busy["id"]).payload)["control"]["goal_id"] == state["id"]
        assert queue.published == [busy["id"], continuation["id"]]


def test_committed_tombstone_is_not_treated_as_a_missing_enqueue(queue):
    busy = queue.send("执行中")
    goal = control.issue(*queue.owner, queue.chat_id, command="/goal 目标", request_key="initial")
    with queue.factory.begin() as session:
        session.get(WorkbenchRun, goal["run"]["id"]).status = "discarded"
    queue.finish(busy["id"])
    assert control.drive_goal(*queue.owner, queue.chat_id) is None
    assert control.read(*queue.owner, queue.chat_id)["goal"]["phase"] == "paused"
