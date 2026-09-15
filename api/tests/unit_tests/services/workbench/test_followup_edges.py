"""Regressions for branch recovery, regeneration and paused-task input contracts."""

import json
from uuid import uuid4

import pytest
from werkzeug.exceptions import Conflict, Forbidden

from models.workbench import WorkbenchChat, WorkbenchRevision, WorkbenchRun
from services.workbench import files, followups, message_actions, service

from . import test_followups

queue = test_followups.queue


def pause(queue, run):
    with queue.factory.begin() as session:
        row = session.get(WorkbenchRun, run["id"])
        row.payload = json.dumps({**json.loads(row.payload), "user_paused": True})
        row.status = "cancelled"


def test_recovery_ignores_an_unrelated_historical_paused_branch(queue):
    old = queue.send("旧分支")
    pause(queue, old)
    current = message_actions.regenerate(*queue.owner, old["id"], 1, "regenerate-branch")
    waiting = queue.send("新分支的后续消息")
    queue.finish(current["id"])
    candidates = followups.waiting_chats()
    # A direct advance succeeds; periodic recovery must also find this chat.
    assert queue.advance() == waiting["id"]
    assert [item[2] for item in candidates] == [current["chat_id"]]


def test_recovery_keeps_the_queue_paused_when_its_own_parent_is_paused(queue):
    current = queue.send("原任务")
    waiting = queue.send("排队消息")
    pause(queue, current)
    assert followups.waiting_chats() == []
    assert queue.advance() is None
    assert queue.get(waiting["id"]).status == "waiting_turn"


@pytest.mark.parametrize("phase", ["idle", "running", "paused"])
def test_attachment_only_send_with_persistent_knowledge_selection(queue, monkeypatch, phase):
    original = queue.send("原任务") if phase != "idle" else None
    if phase == "paused":
        pause(queue, original)
    monkeypatch.setattr(
        service, "compile_config", lambda *_: {"model": "frozen-model", "knowledge": {"sets": [{"id": "kb"}]}}
    )
    monkeypatch.setattr(files, "validate_attachments", lambda *_: (["/workspace/file.pdf"], []))
    args = {"files": [{"path": "/file.pdf", "version": "v1"}], "request_key": "attachment-only"}
    if phase == "paused":
        args["continue_run_id"] = original["id"]
    sent = queue.send("", **args)
    assert sent["status"] == ("waiting_turn" if phase == "running" else "queued")
    assert sent["query"] == ""
    assert sent["attachments"] == [{"path": "file.pdf", "name": "file.pdf"}]
    data = json.loads(queue.get(sent["id"]).payload)
    assert data["effective_soul"]["knowledge"]["sets"] == [{"id": "kb"}]
    assert "/workspace/file.pdf" in files.generation_query(data)


def test_blank_continue_uses_frozen_config_without_compiling_unrelated_current_selection(queue, monkeypatch):
    original = queue.send("使用原模型完成报告")
    pause(queue, original)

    def unavailable_current_selection(*_):
        raise Conflict("新选择的模型不可用；原任务冻结配置仍有效")

    monkeypatch.setattr(service, "compile_config", unavailable_current_selection)
    continued = queue.send("", continue_run_id=original["id"], request_key="continue")
    assert json.loads(queue.get(continued["id"]).payload)["effective_soul"] == {"model": "frozen-model"}


def test_steering_retry_after_lost_response_keeps_the_original_target(queue):
    current = queue.send("原任务")
    queue.running(current["id"])
    waiting = queue.send("调整说明")
    accepted = followups.steer(*queue.owner, waiting["id"], current["id"])
    queue.finish(current["id"])
    next_run = queue.send("后续独立任务")
    assert followups.steer(*queue.owner, waiting["id"], current["id"])["id"] == accepted["id"]
    with pytest.raises(Conflict, match="其他任务"):
        followups.steer(*queue.owner, waiting["id"], next_run["id"])
    assert len(json.loads(queue.get(current["id"]).payload)["steering_messages"]) == 1
    assert not json.loads(queue.get(next_run["id"]).payload).get("steering_messages")


def test_steering_at_final_seal_keeps_the_message_waiting(queue):
    current = queue.send("原任务")
    queue.running(current["id"])
    waiting = queue.send("结束边界调整")
    assert queue.poll(current["id"], action="seal")["sealed"]
    with pytest.raises(Conflict, match="正在结束"):
        followups.steer(*queue.owner, waiting["id"], current["id"])
    assert queue.get(waiting["id"]).status == "waiting_turn"


@pytest.mark.parametrize("edited_query", [None, "编辑后重新执行原任务"])
def test_regenerated_task_accepts_steering_in_the_current_version(queue, edited_query):
    original = queue.send("原任务")
    queue.finish(original["id"])
    current = message_actions.regenerate(*queue.owner, original["id"], 1, "regenerate", query=edited_query)
    queue.running(current["id"])
    waiting = queue.send("把报告改为横版")
    accepted = followups.steer(*queue.owner, waiting["id"], current["id"])
    assert accepted["steer_target_run_id"] == current["id"]


def test_regeneration_does_not_gain_permission_to_queue_behind_an_active_task(queue):
    original = queue.send("原任务")
    queue.finish(original["id"])
    active = queue.send("正在处理的新任务")
    with pytest.raises(Conflict, match="此会话已有任务"):
        message_actions.regenerate(*queue.owner, original["id"], 1, "regenerate")
    assert queue.published == [original["id"], active["id"]]


@pytest.mark.parametrize("failure", [Conflict("附件已改变"), Forbidden("附件不属于当前会话")])
def test_attachment_only_knowledge_send_keeps_file_validation(queue, monkeypatch, failure):
    monkeypatch.setattr(service, "compile_config", lambda *_: {"knowledge": {"sets": [{"id": "kb"}]}})

    def invalid_attachment(*_):
        raise failure

    monkeypatch.setattr(files, "validate_attachments", invalid_attachment)
    with pytest.raises(type(failure), match=failure.description):
        queue.send("", files=[{"path": "invalid.pdf", "version": "old"}], request_key="invalid")
    assert queue.published == []


def test_blank_continue_uses_owned_original_revision_despite_current_config_changes(queue, monkeypatch):
    original = queue.send("冻结的原始任务")
    waiting = [queue.send(name) for name in ("A", "B", "C")]
    pause(queue, original)
    original_row = queue.get(original["id"])
    with queue.factory.begin() as session:
        session.get(WorkbenchChat, original["chat_id"]).version = 2
        session.add(
            WorkbenchRevision(
                id=str(uuid4()),
                chat_id=original["chat_id"],
                tenant_id=queue.owner[0],
                account_id=queue.owner[1],
                version=2,
                selection=json.dumps({"model": "unavailable-new-model"}),
                effective_soul="{}",
            )
        )
    for name in ("template", "read_chat", "compile_config"):
        monkeypatch.setattr(
            service, name, lambda *_: pytest.fail("blank Continue must not read or compile draft resources")
        )
    continued = queue.send(
        "",
        continue_run_id=original["id"],
        request_key="continue",
        resource_mentions={"skills": ["untrusted-new-skill"]},
        inputs={"injected": True},
    )
    row = queue.get(continued["id"])
    data = json.loads(row.payload)
    assert row.revision_id == original_row.revision_id
    assert data["version"] == 1
    assert data["queue_selection"]["model"] == "test-model"
    assert not data.get("inputs")
    assert data["resource_mentions"]["skills"] == []
    assert json.loads(queue.get(waiting[0]["id"]).payload)["branch_parent_run_id"] == continued["id"]
    with queue.factory() as session:
        assert session.get(WorkbenchChat, original["chat_id"]).version == 2
    assert queue.send("", continue_run_id=original["id"], request_key="continue")["id"] == continued["id"]
    assert queue.published == [original["id"], continued["id"]]


def test_blank_continue_still_checks_current_agent_permission(queue, monkeypatch):
    original = queue.send("已授权任务")
    pause(queue, original)

    def revoked(*_):
        raise Forbidden("Agent 权限已撤回")

    monkeypatch.setattr(service, "_authorized_template", revoked)
    with pytest.raises(Forbidden, match="权限已撤回"):
        queue.send("", continue_run_id=original["id"], request_key="continue")
    assert queue.published == [original["id"]]


def test_recovery_filters_fifty_paused_heads_before_limiting_the_batch(queue):
    old = queue.send("历史暂停")
    pause(queue, old)
    current = message_actions.regenerate(*queue.owner, old["id"], 1, "regenerate")
    waiting = queue.send("应被恢复的队首")
    queue.finish(current["id"])
    template = queue.get(current["id"])
    with queue.factory.begin() as session:
        original_chat = session.get(WorkbenchChat, current["chat_id"])
        for _ in range(50):
            chat_id, parent_id, waiting_id = (str(uuid4()) for _ in range(3))
            session.add(
                WorkbenchChat(
                    id=chat_id,
                    tenant_id=template.tenant_id,
                    account_id=template.account_id,
                    agent_id=original_chat.agent_id,
                    app_id=original_chat.app_id,
                    base_snapshot_id=original_chat.base_snapshot_id,
                )
            )
            for run_id, status, data in (
                (parent_id, "cancelled", {"query": "用户已暂停", "user_paused": True}),
                (
                    waiting_id,
                    "waiting_turn",
                    {"query": "等待继续", "queue_order": 1, "branch_parent_run_id": parent_id},
                ),
            ):
                session.add(
                    WorkbenchRun(
                        id=run_id,
                        chat_id=chat_id,
                        tenant_id=template.tenant_id,
                        account_id=template.account_id,
                        revision_id=template.revision_id,
                        request_key=run_id,
                        status=status,
                        payload=json.dumps(data),
                        event_log="[]",
                    )
                )
    assert [item[2] for item in followups.waiting_chats(limit=1)] == [current["chat_id"]]
    assert queue.advance() == waiting["id"]
    assert followups.waiting_chats() == []
