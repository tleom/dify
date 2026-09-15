"""Queue, pause and steering retain one logical task across automatic recovery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from pydantic_ai.messages import UserPromptPart

from models.workbench import WorkbenchRun
from services.workbench import branches, followups, recovery, scheduler

from . import test_followups
from .test_followups import Queue

queue = test_followups.queue


def configure_local_control(monkeypatch: pytest.MonkeyPatch) -> None:
    from tasks import workbench_tasks

    monkeypatch.setattr(workbench_tasks, "fence_remote", lambda *_: True)
    monkeypatch.setattr(scheduler, "release", lambda *_: None)
    monkeypatch.setattr(recovery, "notify", lambda *_: None)


@pytest.fixture(autouse=True)
def local_control(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_local_control(monkeypatch)


def fail(queue: Queue, run_id: str, status: str = "failed") -> bool:
    with queue.factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        run.status, run.error = status, "连接中断"
        marked = recovery.mark_failure(run)
        if marked:
            payload = json.loads(run.payload)
            payload["recovery"]["due_at"] = 1
            run.payload = json.dumps(payload)
        return marked


@pytest.mark.parametrize("ending", ["failed", "interrupted"])
@pytest.mark.parametrize("queue_after_failure", [False, True])
def test_automatic_continuation_precedes_all_three_waiting_messages(
    queue: Queue, ending: str, queue_after_failure: bool
) -> None:
    original = queue.send("完成长报告")
    queue.running(original["id"])
    if queue_after_failure:
        assert fail(queue, original["id"], ending)
    waiting = [queue.send(name) for name in ("A", "B", "C")]
    if not queue_after_failure:
        assert fail(queue, original["id"], ending)
    assert [item["status"] for item in waiting] == ["waiting_turn"] * 3
    assert queue.advance() is None
    assert followups.waiting_chats() == []
    assert original["id"] in {item["id"] for item in followups.snapshot(*queue.owner, original["chat_id"])["runs"]}

    child_id = recovery.continue_failed(original["id"])
    assert child_id
    assert recovery.continue_failed(original["id"]) == child_id
    data = json.loads(queue.get(child_id).payload)
    original_data = json.loads(queue.get(original["id"]).payload)
    assert data["followup_protocol"] == 1
    assert data["queue_selection"] == original_data["queue_selection"]
    assert data["effective_soul"] == original_data["effective_soul"]
    assert data["recovery"]["attempt"] == 1
    assert json.loads(queue.get(waiting[0]["id"]).payload)["branch_parent_run_id"] == child_id
    assert queue.advance() is None
    assert queue.published == [original["id"], child_id]
    queue.finish(child_id)
    assert [item[2] for item in followups.waiting_chats()] == [original["chat_id"]]
    for item in waiting:
        assert queue.advance() == item["id"]
        queue.finish(item["id"])
    assert queue.published == [original["id"], child_id, *[item["id"] for item in waiting]]


@pytest.mark.parametrize("create_successor", [False, True])
def test_pause_cancels_recovery_and_keeps_queue_until_blank_continue(queue: Queue, create_successor: bool) -> None:
    original = queue.send("原目标")
    waiting = queue.send("后续任务")
    assert fail(queue, original["id"])
    child_id = recovery.continue_failed(original["id"]) if create_successor else None
    paused_id = child_id or original["id"]
    targets = recovery.cancel_chain(*queue.owner, original["id"])
    assert paused_id in targets
    assert json.loads(queue.get(paused_id).payload)["user_paused"]
    assert recovery.continue_failed(paused_id) is None
    assert queue.advance() is None
    assert followups.waiting_chats() == []
    continued = queue.send("", continue_run_id=paused_id, request_key="blank-continue")
    assert continued["is_continuation"]
    assert json.loads(queue.get(waiting["id"]).payload)["branch_parent_run_id"] == continued["id"]
    queue.finish(continued["id"])
    assert queue.advance() == waiting["id"]


def test_steering_during_recovery_is_kept_in_the_successors_context(queue: Queue) -> None:
    original = queue.send("原目标")
    queue.running(original["id"])
    assert fail(queue, original["id"])
    adjustment = queue.send("把报告改成横版")
    assert followups.steer(*queue.owner, adjustment["id"], original["id"])["status"] == "steered"
    child_id = recovery.continue_failed(original["id"])
    assert child_id
    with queue.factory() as session:
        from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState

        history = PydanticAIHistoryRuntimeState.model_validate(
            branches.output_history(session, session.get(WorkbenchRun, original["id"]))
        )
    prompts = [
        part.content for message in history.messages for part in message.parts if isinstance(part, UserPromptPart)
    ]
    assert sum("把报告改成横版" in prompt for prompt in prompts) == 1
    assert any("原目标" in prompt for prompt in prompts)
    queue.running(child_id)
    next_adjustment = queue.send("附上核对清单")
    assert followups.steer(*queue.owner, next_adjustment["id"], child_id)["steer_target_run_id"] == child_id


def test_exhausted_recovery_releases_the_queue_after_three_continuations(queue: Queue) -> None:
    original = queue.send("原目标")
    waiting = queue.send("后续任务")
    current = original["id"]
    for attempt in range(1, 4):
        assert fail(queue, current)
        assert queue.advance() is None
        current = recovery.continue_failed(current)
        assert current
        assert json.loads(queue.get(current).payload)["recovery"]["attempt"] == attempt
    assert not fail(queue, current)
    assert queue.advance() == waiting["id"]


def test_removing_the_only_waiting_message_does_not_cancel_recovery(queue: Queue) -> None:
    original = queue.send("原目标")
    waiting = queue.send("待删除消息")
    assert fail(queue, original["id"])
    followups.remove(*queue.owner, waiting["id"])
    assert recovery.continue_failed(original["id"])


@pytest.mark.parametrize("continuation", ["automatic", "manual"])
@pytest.mark.parametrize("ending", ["failed", "interrupted"])
def test_waiting_task_retains_its_own_recovery_after_a_newer_ancestor(
    queue: Queue, continuation: str, ending: str
) -> None:
    original = queue.send("任务 A")
    waiting, last = [queue.send(query) for query in ("任务 B", "任务 C")]
    if continuation == "automatic":
        assert fail(queue, original["id"])
        ancestor_id = recovery.continue_failed(original["id"])
    else:
        recovery.cancel_chain(*queue.owner, original["id"])
        ancestor_id = queue.send("", continue_run_id=original["id"], request_key="continue-a")["id"]
    assert ancestor_id
    # The waiting rows were created before the resumed ancestor, even though
    # their execution must follow it. Fix the clock to exercise that ordering.
    with queue.factory.begin() as session:
        for seconds, run_id in enumerate((original["id"], waiting["id"], last["id"], ancestor_id)):
            stored_row = session.get(WorkbenchRun, run_id)
            assert stored_row is not None
            stored_row.created_at = datetime(2026, 9, 16) + timedelta(seconds=seconds)
    queue.finish(ancestor_id)
    assert queue.advance() == waiting["id"]
    queue.running(waiting["id"])
    assert fail(queue, waiting["id"], ending)
    successor = recovery.continue_failed(waiting["id"])
    assert successor
    assert json.loads(queue.get(successor).payload)["recovery"]["attempt"] == 1
    assert json.loads(queue.get(last["id"]).payload)["branch_parent_run_id"] == successor
    queue.finish(successor)
    assert queue.advance() == last["id"]
