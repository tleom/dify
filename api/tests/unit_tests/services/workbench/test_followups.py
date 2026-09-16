"""Persisted FIFO and steering races against real SQLAlchemy transactions."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import starmap
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.exceptions import Conflict, Forbidden, NotFound

from core.db import session_factory as factory_module
from models.agent import AgentWorkspaceBinding
from models.model import Conversation
from models.workbench import WorkbenchChat, WorkbenchRevision, WorkbenchRun
from services.workbench import followups, scheduler, service


class RunData(TypedDict):
    id: str
    chat_id: str
    query: str
    status: str
    queue_order: int
    parent_run_id: str | None
    is_continuation: bool
    attachments: list[dict[str, str]]


class FollowupBatch(TypedDict):
    messages: list[dict[str, str]]
    sealed: bool


@dataclass
class Queue:
    owner: tuple[str, str]
    chat_id: str
    app_id: str
    factory: sessionmaker[Session]
    published: list[str]
    admin: Engine | None = None
    application_name: str = ""

    def send(self, query: str, **changes: object) -> RunData:
        key = changes.pop("request_key", query)
        assert isinstance(key, str)
        return cast(
            RunData,
            service.enqueue(*self.owner, self.chat_id, 1, key, {"query": query, "queue_when_busy": True, **changes}),
        )

    def get(self, run_id: str) -> WorkbenchRun:
        with self.factory() as session:
            run = session.get(WorkbenchRun, run_id)
            assert run is not None
            return run

    def finish(self, run_id: str, status: str = "completed") -> None:
        with self.factory.begin() as session:
            run = session.get(WorkbenchRun, run_id)
            assert run is not None
            run.status = status

    def running(self, run_id: str) -> None:
        with self.factory.begin() as session:
            run = session.get(WorkbenchRun, run_id)
            assert run is not None
            run.status, run.backend_run_id = "running", str(uuid4())

    def advance(self) -> str | None:
        return followups.advance(*self.owner, self.chat_id)

    def poll(
        self, run_id: str, *, action: Literal["poll", "seal"] = "poll", seen_ids: list[str] | None = None
    ) -> FollowupBatch:
        ticket = self.get(run_id).backend_run_id
        assert ticket is not None
        return cast(
            FollowupBatch,
            followups.poll(
                followups.AgentFollowupsPayload(
                    tenant_id=self.owner[0],
                    account_id=self.owner[1],
                    app_id=self.app_id,
                    workbench_run_id=run_id,
                    backend_run_id=ticket,
                    action=action,
                    seen_ids=seen_ids or [],
                )
            ),
        )


def queue_fixture(
    monkeypatch: pytest.MonkeyPatch, config_overrides: Callable[..., None], tmp_path: Path
) -> Iterator[Queue]:
    config_overrides(WORKBENCH_ENABLED=True, WORKBENCH_ACTIVITY_ENABLED=False)
    # DTO enrichment opens a separate read session. A file database gives it a
    # separate connection, so closing it cannot roll back the writer's transaction.
    engine = create_engine(f"sqlite:///{(tmp_path / 'queue.sqlite').as_posix()}")
    WorkbenchChat.metadata.create_all(
        engine,
        tables=[
            WorkbenchChat.metadata.tables[model.__tablename__]
            for model in (WorkbenchChat, WorkbenchRevision, WorkbenchRun, AgentWorkspaceBinding, Conversation)
        ],
    )
    from models.workbench import WorkbenchCommand, WorkbenchControl, WorkbenchRunEvent

    WorkbenchControl.metadata.create_all(
        engine,
        tables=[
            WorkbenchControl.metadata.tables[model.__tablename__]
            for model in (WorkbenchControl, WorkbenchCommand, WorkbenchRunEvent)
        ],
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(factory_module, "_session_maker", factory)
    tenant, account, agent, app, snapshot, chat_id, revision = [str(uuid4()) for _ in range(7)]
    selection: dict[str, str | list[str]] = {"model": "test-model", "skills": [], "tools": [], "knowledge": []}
    with factory.begin() as session:
        session.add(
            WorkbenchChat(
                id=chat_id, tenant_id=tenant, account_id=account, agent_id=agent, app_id=app, base_snapshot_id=snapshot
            )
        )
        session.add(
            WorkbenchRevision(
                id=revision,
                chat_id=chat_id,
                tenant_id=tenant,
                account_id=account,
                version=1,
                selection=json.dumps(selection),
                effective_soul="{}",
            )
        )
    monkeypatch.setattr(service, "authorize", lambda *_: None)
    monkeypatch.setattr(service, "template", lambda *_: {"agent_id": agent, "snapshot_id": snapshot, "soul": {}})
    monkeypatch.setattr(
        service, "_authorized_template", lambda *_: {"agent_id": agent, "snapshot_id": snapshot, "soul": {}}
    )
    monkeypatch.setattr(service, "read_chat", lambda *_: {"version": 1, "selection": selection})
    monkeypatch.setattr(service, "compile_config", lambda *_: {"model": "frozen-model"})
    from services.workbench import personal_mcp

    monkeypatch.setattr(personal_mcp, "catalog", lambda *_: [])
    published: list[str] = []
    monkeypatch.setattr(scheduler, "publish", lambda *args: published.append(args[-1]))

    yield Queue((tenant, account), chat_id, app, factory, published)
    engine.dispose()


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch, config_overrides: Callable[..., None], tmp_path: Path) -> Iterator[Queue]:
    yield from queue_fixture(monkeypatch, config_overrides, tmp_path)


@pytest.mark.parametrize("ending", ["completed", "failed", "cancelled", "interrupted"])
def test_three_waiting_messages_follow_fifo_and_publish_only_after_predecessor(queue: Queue, ending: str) -> None:
    first = queue.send("原任务")
    waiting = [queue.send(f"补充 {index}") for index in range(3)]
    assert [item["status"] for item in waiting] == ["waiting_turn"] * 3
    assert [item["queue_order"] for item in waiting] == [1, 2, 3]
    assert queue.published == [first["id"]]
    with pytest.raises(Conflict, match="最多排队"):
        queue.send("第四条")
    assert queue.send("补充 1")["id"] == waiting[1]["id"]
    assert queue.advance() is None
    previous = first
    for item in waiting:
        queue.finish(previous["id"], ending)
        assert queue.advance() == item["id"]
        assert queue.get(item["id"]).status == "queued"
        assert queue.advance() is None
        previous = item
    assert queue.published == [item["id"] for item in [first, *waiting]]


def test_edit_removes_middle_rewires_children_and_resend_goes_to_tail(queue: Queue) -> None:
    first = queue.send("原任务")
    a, b, c = [queue.send(name) for name in ("A", "B", "C")]
    removed = followups.remove(*queue.owner, b["id"])
    assert removed["query"] == "B"
    assert followups.remove(*queue.owner, b["id"])["status"] == "discarded"
    assert json.loads(queue.get(c["id"]).payload)["branch_parent_run_id"] == a["id"]
    edited = queue.send("B 编辑后")
    assert edited["queue_order"] == 4
    assert edited["parent_run_id"] == c["id"]
    queue.finish(first["id"])
    assert queue.advance() == a["id"]


@pytest.mark.parametrize("query", ["", "把原报告改为横版"])
def test_paused_queue_waits_and_manual_continuation_precedes_all_three_messages(queue: Queue, query: str) -> None:
    from services.workbench.branches import output_history

    original = queue.send("生成原报告")
    queue.running(original["id"])
    waiting = [queue.send(name) for name in ("A", "B", "C")]
    saved_history: dict[str, list[object]] = {"messages": []}
    with queue.factory.begin() as session:
        source = session.get(WorkbenchRun, original["id"])
        assert source is not None
        data = json.loads(source.payload)
        data.update(user_paused=True, output_history=saved_history)
        source.payload, source.status = json.dumps(data), "cancelled"
    assert queue.advance() is None
    assert followups.waiting_chats() == []
    assert queue.published == [original["id"]]

    resumed = queue.send(query, continue_run_id=original["id"], request_key="manual-continue")
    assert resumed["status"] == "queued"
    assert resumed["query"] == (query or "继续")
    assert resumed["is_continuation"] is (not query)
    assert resumed["parent_run_id"] == original["id"]
    assert queue.published == [original["id"], resumed["id"]]
    assert queue.send(query, continue_run_id=original["id"], request_key="manual-continue")["id"] == resumed["id"]
    with queue.factory() as session:
        assert output_history(session, session.get(WorkbenchRun, original["id"])) == saved_history
    assert json.loads(queue.get(waiting[0]["id"]).payload)["branch_parent_run_id"] == resumed["id"]
    assert queue.advance() is None
    previous = resumed
    for item in waiting:
        queue.finish(previous["id"])
        assert queue.advance() == item["id"]
        previous = item
    assert queue.published == [original["id"], resumed["id"], *[item["id"] for item in waiting]]


def test_continuation_rejects_a_changed_or_foreign_target_and_preserves_queue(queue: Queue) -> None:
    original = queue.send("原报告")
    waiting = queue.send("A")
    with pytest.raises(NotFound):
        queue.send("", continue_run_id=str(uuid4()), request_key="foreign")
    with pytest.raises(Conflict, match="状态改变"):
        queue.send("", continue_run_id=original["id"], request_key="still-running")
    queue.finish(original["id"], "cancelled")
    resumed = queue.send("", continue_run_id=original["id"], request_key="first")
    with pytest.raises(Conflict, match="状态改变"):
        queue.send("另一标签页的新内容", continue_run_id=original["id"], request_key="second")
    assert queue.get(waiting["id"]).status == "waiting_turn"
    assert queue.published == [original["id"], resumed["id"]]


def test_continuation_retains_original_query_when_paused_before_first_model_call(queue: Queue) -> None:
    from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState
    from pydantic_ai.messages import UserPromptPart

    from services.workbench.branches import output_history

    original = queue.send("还没开始执行的原始目标")
    queue.finish(original["id"], "cancelled")
    with queue.factory() as session:
        state = PydanticAIHistoryRuntimeState.model_validate(
            output_history(session, session.get(WorkbenchRun, original["id"]))
        )
    assert any(
        part.content == "还没开始执行的原始目标"
        for message in state.messages
        for part in message.parts
        if isinstance(part, UserPromptPart)
    )
    history = state.model_dump(mode="json")
    assert followups.carry_unseen_history(history, [json.loads(queue.get(original["id"]).payload)]) == history


def test_continuation_cannot_move_another_branches_waiting_messages(queue: Queue) -> None:
    old = queue.send("旧分支")
    queue.finish(old["id"], "cancelled")
    current = queue.send("当前分支")
    waiting = queue.send("当前分支的排队消息")
    queue.finish(current["id"], "cancelled")
    with pytest.raises(Conflict, match="另一个任务"):
        queue.send("", continue_run_id=old["id"], request_key="old-branch")
    assert json.loads(queue.get(waiting["id"]).payload)["branch_parent_run_id"] == current["id"]
    assert queue.published == [old["id"], current["id"]]


def test_fenced_context_and_delivery_cursor_are_saved_before_continuation(queue: Queue) -> None:
    original = queue.send("原始目标")
    queue.running(original["id"])
    queued = queue.send("已经消费的补充")
    followups.steer(*queue.owner, queued["id"], original["id"])
    queue.finish(original["id"], "cancelled")
    followups.save_fenced_state(
        queue.get(original["id"]).backend_run_id,
        {
            "history": {"messages": []},
            "steering_delivered_ids": [queued["id"]],
        },
    )
    data = json.loads(queue.get(original["id"]).payload)
    assert data["output_history"] == {"messages": []}
    assert data["steering_delivered_ids"] == [queued["id"]]
    assert len(data["steering_messages"]) == 1
    assert followups.carry_unseen_history(data["output_history"], [data]) == {"messages": []}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "another-model"),
        ("skills", ["another-skill"]),
        ("tools", ["another-tool"]),
        ("knowledge", ["another-dataset"]),
        ("model_parameters", {"temperature": 0.2}),
        ("tool_parameters", {"tool": {"mode": "other"}}),
        ("effective_soul", {"model": "another-frozen-model"}),
    ],
)
def test_steering_retains_queue_when_frozen_configuration_differs(queue: Queue, field: str, value: object) -> None:
    original = queue.send("原任务")
    queue.running(original["id"])
    pending, tail = [queue.send(query) for query in ("补充内容", "随后执行")]
    with queue.factory.begin() as session:
        message = session.get(WorkbenchRun, pending["id"])
        assert message is not None
        payload = json.loads(message.payload)
        if field == "effective_soul":
            payload[field] = value
        else:
            payload["queue_selection"][field] = value
        message.payload = json.dumps(payload)
    before = queue.get(original["id"]).payload
    with pytest.raises(Conflict, match="配置"):
        followups.steer(*queue.owner, pending["id"], original["id"])
    assert queue.get(pending["id"]).status == "waiting_turn"
    assert queue.get(original["id"]).payload == before
    assert json.loads(queue.get(tail["id"]).payload)["branch_parent_run_id"] == pending["id"]


def test_stop_cannot_turn_a_waiting_message_into_a_paused_queue_parent(queue: Queue) -> None:
    from services.workbench.recovery import cancel_chain

    original = queue.send("原任务")
    pending, tail = [queue.send(query) for query in ("排队第一条", "排队第二条")]
    with pytest.raises(Conflict, match="队列移除"):
        cancel_chain(*queue.owner, pending["id"])
    assert queue.get(pending["id"]).status == "waiting_turn"
    assert not json.loads(queue.get(pending["id"]).payload).get("user_paused")
    queue.finish(original["id"])
    assert queue.advance() == pending["id"]
    queue.finish(pending["id"])
    assert queue.advance() == tail["id"]


def test_steer_any_position_joins_actual_running_task_once_and_seals_atomically(queue: Queue) -> None:
    first = queue.send("原任务")
    queue.running(first["id"])
    a, b, c = [queue.send(name) for name in ("A", "B", "C")]
    for _ in range(2):
        assert followups.steer(*queue.owner, c["id"], first["id"])["steer_target_run_id"] == first["id"]
    batch = queue.poll(first["id"], action="seal")
    assert batch["sealed"] is False
    assert [item["id"] for item in batch["messages"]] == [c["id"]]
    assert batch["messages"][0]["content"].startswith(followups.STEERING_PREFIX)
    assert queue.poll(first["id"]) == batch
    assert queue.poll(first["id"], seen_ids=[c["id"]], action="seal") == {"messages": [], "sealed": True}
    with pytest.raises(Conflict, match="正在结束"):
        followups.steer(*queue.owner, b["id"], first["id"])
    assert queue.get(b["id"]).status == "waiting_turn"
    queue.finish(first["id"])
    assert queue.advance() == a["id"]
    assert queue.get(c["id"]).status == "steered"


def test_cross_owner_and_execution_ticket_cannot_consume_or_steer(queue: Queue) -> None:
    first = queue.send("原任务")
    queue.running(first["id"])
    pending = queue.send("补充")
    with pytest.raises(NotFound):
        followups.steer(queue.owner[0], str(uuid4()), pending["id"], first["id"])
    with pytest.raises(NotFound):
        followups.remove(str(uuid4()), queue.owner[1], pending["id"])
    current = queue.get(first["id"])
    assert current.backend_run_id is not None
    with pytest.raises(Forbidden):
        followups.poll(
            followups.AgentFollowupsPayload(
                tenant_id=queue.owner[0],
                account_id=queue.owner[1],
                app_id=str(uuid4()),
                workbench_run_id=current.id,
                backend_run_id=current.backend_run_id,
            )
        )
    assert queue.get(pending["id"]).status == "waiting_turn"


def test_paused_current_task_retains_queue_until_it_finishes(queue: Queue) -> None:
    first = queue.send("原任务")
    queued = queue.send("下一条")
    for state in ("waiting_input", "environment_update", "environment_installing", "stopping"):
        queue.finish(first["id"], state)
        assert queue.advance() is None
        assert queue.get(queued["id"]).status == "waiting_turn"


def test_accepted_input_survives_cancellation_before_delivery_without_replay_after_compaction(queue: Queue) -> None:
    from services.workbench.branches import output_history

    first = queue.send("生成报告")
    queued = queue.send("横版报告")
    followups.steer(*queue.owner, queued["id"], first["id"])
    queue.finish(first["id"], "cancelled")
    with queue.factory() as session:
        history = output_history(session, session.get(WorkbenchRun, first["id"]))
    assert "横版报告" in json.dumps(history, ensure_ascii=False)
    payload = json.loads(queue.get(first["id"]).payload)
    assert followups.carry_unseen_history(history, [payload]) == history
    payload["steering_delivered_ids"] = [queued["id"]]
    compacted: dict[str, list[object]] = {"messages": []}
    assert followups.carry_unseen_history(compacted, [payload]) == compacted


def test_delivery_cursor_does_not_limit_total_supplements_to_a_long_running_task() -> None:
    payload = followups.AgentFollowupsPayload(
        tenant_id="tenant",
        account_id="account",
        app_id="app",
        workbench_run_id="run",
        backend_run_id="native",
        seen_ids=[str(index) for index in range(1100)],
    )
    assert len(payload.seen_ids) == 1100


def test_a_delayed_steer_click_must_not_silently_target_the_next_task(queue: Queue) -> None:
    first = queue.send("处理合同 A")
    queue.running(first["id"])
    second = queue.send("独立处理合同 B")
    correction = queue.send("把合同 A 的期限改成 10 天")
    # The UI still displays first when the click is made. Before the request is
    # handled, first ends and the server admits second. The captured target must
    # reject the delayed request and preserve the correction in the queue.
    queue.finish(first["id"])
    assert queue.advance() == second["id"]
    with pytest.raises(Conflict):
        followups.steer(*queue.owner, correction["id"], first["id"])


def test_reconciliation_must_find_eligible_chat_behind_fifty_paused_chats(queue: Queue) -> None:
    seed = queue.send("种子")
    template = queue.get(seed["id"])
    parents = {}
    with queue.factory.begin() as session:
        original = session.get(WorkbenchChat, template.chat_id)
        assert original is not None
        for index in range(51):
            chat_id, parent_id, waiting_id = (str(uuid4()) for _ in range(3))
            session.add(
                WorkbenchChat(
                    id=chat_id,
                    tenant_id=original.tenant_id,
                    account_id=original.account_id,
                    agent_id=original.agent_id,
                    app_id=original.app_id,
                    base_snapshot_id=original.base_snapshot_id,
                )
            )
            for run_id, status, data in (
                (parent_id, "waiting_input", {"query": f"父任务{index}"}),
                (
                    waiting_id,
                    "waiting_turn",
                    {"query": f"下一条{index}", "queue_order": 1, "branch_parent_run_id": parent_id},
                ),
            ):
                session.add(
                    WorkbenchRun(
                        id=run_id,
                        tenant_id=original.tenant_id,
                        account_id=original.account_id,
                        chat_id=chat_id,
                        revision_id=template.revision_id,
                        request_key=run_id,
                        status=status,
                        payload=json.dumps(data),
                        event_log="[]",
                    )
                )
            parents[chat_id] = parent_id
    sampled = {item[2] for item in followups.waiting_chats()}
    omitted = next(chat_id for chat_id in parents if chat_id not in sampled)
    queue.finish(parents[omitted])
    # Normal completion publication was lost. Periodic recovery is the remaining path.
    assert omitted in {item[2] for item in followups.waiting_chats()}
    for parent_id in parents.values():
        queue.finish(parent_id)
    first_batch = followups.waiting_chats()
    assert len(first_batch) == 50
    admitted = set(starmap(followups.advance, first_batch))
    second_batch = followups.waiting_chats()
    assert len(second_batch) == 1
    admitted.update(starmap(followups.advance, second_batch))
    assert len(admitted) == 51
    assert None not in admitted
    assert followups.waiting_chats() == []


def test_committed_retry_survives_missing_configuration_and_changed_attachment(
    queue: Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = queue.send("不重复执行", request_key="stable-message")
    monkeypatch.setattr(service, "template", lambda *_: pytest.fail("a committed retry must not rediscover providers"))
    duplicate = queue.send("不重复执行", request_key="stable-message", files=[{"path": "gone", "version": "old"}])
    assert duplicate["id"] == original["id"]
    assert queue.published == [original["id"]]


def test_lightweight_snapshot_excludes_history_and_preserves_tracked_removed_outcomes(
    queue: Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import event as sql_event

    from services.workbench import event_log

    original = queue.send("历史任务")
    queue.finish(original["id"])
    current = queue.send("当前任务")
    waiting = queue.send("排队消息")
    removed = queue.send("删除后仍能核对")
    followups.remove(*queue.owner, removed["id"])
    with queue.factory.begin() as session:
        run = session.get(WorkbenchRun, current["id"])
        assert run is not None
        data = json.loads(run.payload)
        data["activity_protocol"] = 1
        run.payload = json.dumps(data)
        run.event_log = "invalid history must never be loaded"
    monkeypatch.setattr(event_log, "history_snapshot", lambda *_: pytest.fail("polling must not read a journal"))
    statements = []
    engine = queue.factory.kw["bind"]

    def capture(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)

    sql_event.listen(engine, "before_cursor_execute", capture)
    try:
        result = followups.snapshot(*queue.owner, current["chat_id"], [current["id"], removed["id"]])
    finally:
        sql_event.remove(engine, "before_cursor_execute", capture)
    assert {run["id"] for run in result["runs"]} == {current["id"], waiting["id"], removed["id"]}
    assert all(run["events"] == [] for run in result["runs"])
    assert not any("event_log" in statement or "workbench_run_events" in statement for statement in statements)
    with pytest.raises(NotFound):
        followups.snapshot(queue.owner[0], str(uuid4()), current["chat_id"], [current["id"]])


@pytest.mark.parametrize("ordering", ["before", "after"])
def test_context_updates_preserve_serialized_steering_and_final_seal(
    queue: Queue, monkeypatch: pytest.MonkeyPatch, ordering: str
) -> None:
    from types import SimpleNamespace

    from sqlalchemy.dialects import postgresql

    from services.workbench import context_status

    from .test_context_status import event

    first = queue.send("原任务")
    queue.running(first["id"])
    pending = queue.send("不能丢失的调整")
    conversation_id = str(uuid4())
    with queue.factory.begin() as session:
        run = session.get(WorkbenchRun, first["id"])
        assert run is not None
        stored_row = session.get(WorkbenchChat, run.chat_id)
        assert stored_row is not None
        stored_row.conversation_id = conversation_id
        payload = json.loads(run.payload)
        payload["effective_soul"] = {"model": {"model_provider": "test", "model": "test"}}
        run.payload = json.dumps(payload)
        message = session.get(WorkbenchRun, pending["id"])
        assert message is not None
        message_payload = json.loads(message.payload)
        message_payload["effective_soul"] = payload["effective_soul"]
        message.payload = json.dumps(message_payload)
    original_current_run = context_status.current_run

    def locked_current(
        session: Session, tenant_id: str, conversation_id: str, account_id: str, *, for_update: bool = False
    ) -> WorkbenchRun | None:
        assert for_update is True
        # SQLite does not implement row locking. Inspect the PostgreSQL statement
        # and verify both legal serial orders; live locking belongs to CI integration.
        from unittest.mock import Mock

        recorder = Mock()
        original_current_run(recorder, tenant_id, conversation_id, account_id, for_update=for_update)
        sql = str(recorder.scalar.call_args.args[0].compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE OF workbench_runs" in sql
        return original_current_run(session, tenant_id, conversation_id, account_id, for_update=for_update)

    monkeypatch.setattr(context_status, "current_run", locked_current)
    monkeypatch.setattr(
        context_status, "redis_client", SimpleNamespace(xadd=lambda *_: b"100-0", expire=lambda *_: None)
    )

    def record() -> None:
        update = event().model_copy(update={"run_id": queue.get(first["id"]).backend_run_id})
        context_status.record_context_status(queue.owner[0], conversation_id, queue.owner[1], update)

    if ordering == "before":
        record()
    followups.steer(*queue.owner, pending["id"], first["id"])
    assert queue.poll(first["id"], seen_ids=[pending["id"]], action="seal")["sealed"]
    if ordering == "after":
        record()
    data = json.loads(queue.get(first["id"]).payload)
    assert [item["id"] for item in data["steering_messages"]] == [pending["id"]]
    assert data["steering_closed_ticket"] == queue.get(first["id"]).backend_run_id
    assert data["context_usage"]["used_tokens"] == 500
