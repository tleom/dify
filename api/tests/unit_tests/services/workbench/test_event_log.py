"""SQLite exercises persisted event order/ownership, with Redis failure isolated at the wake-up boundary."""

import json
from collections.abc import Callable, Iterator
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from agenton.compositor import CompositorSessionSnapshot
from dify_agent.protocol.schemas import DeferredToolCallPayload
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.exceptions import NotFound

from clients.agent_backend import AgentBackendDeferredToolCallInternalEvent
from core.db import session_factory as factory_module
from models.agent import AgentConfigVersionKind, AgentWorkspaceBinding
from models.base import TypeBase
from models.workbench import WorkbenchChat, WorkbenchRun, WorkbenchRunEvent
from services.workbench import event_log

_REAL_NOTIFY = event_log.notify
type Journal = tuple[sessionmaker[Session], str, str, str, str]


@pytest.fixture
def journal(monkeypatch: pytest.MonkeyPatch) -> Iterator[Journal]:
    engine = create_engine("sqlite://")
    TypeBase.metadata.create_all(
        engine,
        tables=[
            TypeBase.metadata.tables[model.__tablename__]
            for model in (
                WorkbenchChat,
                WorkbenchRun,
                WorkbenchRunEvent,
                AgentWorkspaceBinding,
            )
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(factory_module, "_session_maker", factory)
    monkeypatch.setattr(event_log, "notify", lambda *_: None)
    tenant, account, run_id, chat_id = (str(uuid4()) for _ in range(4))
    with factory.begin() as session:
        session.add(
            WorkbenchChat(
                id=chat_id,
                tenant_id=tenant,
                account_id=account,
                agent_id=str(uuid4()),
                app_id=str(uuid4()),
                base_snapshot_id=str(uuid4()),
            )
        )
        session.add(
            WorkbenchRun(
                id=run_id,
                chat_id=chat_id,
                tenant_id=tenant,
                account_id=account,
                revision_id=str(uuid4()),
                request_key="request",
                payload=json.dumps({"activity_protocol": 1, "pending": {"tool_call_id": "human"}}),
                status="running",
                backend_run_id=str(uuid4()),
            )
        )
    yield factory, tenant, account, run_id, chat_id
    engine.dispose()


def test_history_live_and_reconnect_share_one_durable_order(journal: Journal) -> None:
    factory, tenant, account, run_id, _ = journal
    events = [
        {"event": "agent_message", "message_id": "message", "answer": "开始"},
        {"event": "workbench_activity", "data": {"kind": "activity", "title": "安装依赖以读取文档"}},
        {"event": "workbench_knowledge", "search_id": "search", "status": "returned"},
        {"event": "workbench_context", "phase": "compacted", "used_tokens": 30},
        {"event": "workbench_activity", "data": {"kind": "tool", "stage": "returned"}},
    ]
    for index, item in enumerate(events, 1):
        assert event_log.append_event(run_id, item) == f"{index}-0"
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        run.status = "completed"
        saved = event_log.history_events(run)
        payload = json.loads(run.payload)
        assert payload["pending"] == {"tool_call_id": "human"}
        assert payload["message_ids"] == ["message"]
        assert payload["context_usage"]["_id"] == "4-0"
        # The legacy executor's JSON field is not the new journal's authority.
        run.event_log = "[]"
    live = list(event_log.stream_events(tenant, account, run_id))
    assert live[:-1] == saved
    terminal = live[-1]
    assert terminal is not None
    assert terminal["status"] == "completed"
    assert list(event_log.stream_events(tenant, account, run_id, after=2))[:-1] == saved[2:]


def test_idempotent_source_and_native_attempt_validation(journal: Journal) -> None:
    factory, tenant, account, run_id, _ = journal
    with factory() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        ticket = run.backend_run_id
    item: dict[str, object] = {
        "event": "workbench_activity",
        "backend_run_id": ticket,
        "source_event_id": "1-0",
        "data": {},
    }
    assert event_log.append_event(run_id, item, expected_backend_run_id=ticket) == "1-0"
    assert event_log.append_event(run_id, item, expected_backend_run_id=ticket) == "1-0"
    assert event_log.append_event(run_id, item, expected_backend_run_id="old") is None
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        run.status = "cancelled"
    assert event_log.append_event(run_id, {**item, "source_event_id": "2-0"}) is None
    assert len(event_log.read_page(tenant, account, run_id)[0]) == 1


@pytest.mark.parametrize("boundary", ["tenant", "account", "chat_tenant", "chat_account", "deleted"])
def test_every_page_rechecks_full_ownership(journal: Journal, boundary: str) -> None:
    factory, tenant, account, run_id, chat_id = journal
    event_log.append_event(run_id, {"event": "agent_message", "answer": "private"})
    if boundary in {"tenant", "account"}:
        if boundary == "tenant":
            tenant = str(uuid4())
        else:
            account = str(uuid4())
    else:
        with factory.begin() as session:
            chat = session.get(WorkbenchChat, chat_id)
            setattr(
                chat,
                {"chat_tenant": "tenant_id", "chat_account": "account_id", "deleted": "deleted"}[boundary],
                1 if boundary == "deleted" else str(uuid4()),
            )
    with pytest.raises(NotFound):
        event_log.read_page(tenant, account, run_id)
    with pytest.raises(NotFound):
        event_log.read_state(tenant, account, run_id)


def test_events_endpoint_reconnects_without_materializing_the_full_journal(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flask import Flask

    from controllers.console import workbench

    factory, tenant, account, run_id, _ = journal
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        for index in range(3):
            event_log.append_locked(session, run, {"event": "agent_message", "answer": f"{index}:" + "x" * 100_000})
        run.status = "completed"

    def unexpected_history(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("SSE must read only pages after the requested cursor")

    monkeypatch.setattr(workbench.WorkbenchResource, "owner", lambda _self: (tenant, account))
    monkeypatch.setattr(workbench, "owned_run", unexpected_history)
    monkeypatch.setattr(event_log, "history_events", unexpected_history)
    with Flask(__name__).test_request_context(headers={"Last-Event-ID": "2-0"}):
        response = workbench.Events().get(UUID(run_id))
        text = response.get_data(as_text=True)
    assert "id: 3-0\n" in text
    assert "id: 1-0\n" not in text
    assert "id: 2-0\n" not in text
    assert '"status": "completed"' in text


def test_terminal_stream_drains_all_pages_and_ignores_old_attempt_end(journal: Journal) -> None:
    factory, tenant, account, run_id, _ = journal
    with factory.begin() as session:
        run = session.scalar(select(WorkbenchRun).where(WorkbenchRun.id == run_id).with_for_update())
        assert run is not None
        for index in range(105):
            event_log.append_locked(session, run, {"event": "agent_message", "answer": str(index)})
        event_log.append_locked(session, run, {"event": "workbench_end", "status": "environment_update"})
        event_log.append_locked(session, run, {"event": "agent_message", "answer": "安装后验证"})
        run.status = "completed"
    stream = list(event_log.stream_events(tenant, account, run_id))
    assert len(stream) == 107
    answer, terminal = stream[-2:]
    assert answer is not None
    assert terminal is not None
    assert answer["answer"] == "安装后验证"
    assert terminal["status"] == "completed"


@pytest.mark.parametrize("pause_status", ["waiting_input", "environment_update"])
@pytest.mark.parametrize("status", ["queued", "running", "completed"])
def test_history_dto_excludes_previous_attempt_endings(journal: Journal, pause_status: str, status: str) -> None:
    from services.workbench.service import run_dto

    factory, tenant, account, run_id, _ = journal
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        event_log.append_locked(session, run, {"event": "agent_message", "answer": "暂停前"})
        event_log.append_locked(session, run, {"event": "workbench_end", "status": pause_status})
        if status != "queued":
            event_log.append_locked(session, run, {"event": "agent_message", "answer": "继续执行"})
        if status == "completed":
            event_log.append_locked(session, run, {"event": "workbench_end", "status": status})
        run.status = status
        dto = run_dto(run)
        assert dto["status"] == status
        assert [item["answer"] for item in dto["events"]] == (
            ["暂停前"] if status == "queued" else ["暂停前", "继续执行"]
        )
    if status == "completed":
        assert list(event_log.stream_events(tenant, account, run_id))[:-1] == dto["events"]


@pytest.mark.parametrize("retrieval_status", ["returned", "error"])
@pytest.mark.parametrize("consumer_event", ["tool", "end"])
def test_knowledge_callback_cannot_overtake_queued_tool_start(
    journal: Journal, config_overrides: Callable[..., None], retrieval_status: str, consumer_event: str
) -> None:
    from services.entities.knowledge_retrieval_inner import InnerKnowledgeRetrieveRequest
    from services.workbench.knowledge_events import retrieval_event

    config_overrides(WORKBENCH_ENABLED=True)
    factory, tenant, account, run_id, chat_id = journal
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        chat = session.get(WorkbenchChat, chat_id)
        assert run is not None
        assert chat is not None
        ticket, app_id = run.backend_run_id, chat.app_id
        payload = json.loads(run.payload)
        payload["effective_soul"] = {
            "knowledge": {
                "sets": [
                    {
                        "id": "kb",
                        "name": "知识库",
                        "datasets": [{"id": "dataset"}],
                        "query": {"mode": "generated_query"},
                    }
                ]
            }
        }
        run.payload = json.dumps(payload)
    request = InnerKnowledgeRetrieveRequest.model_validate(
        {
            "workbench_run_id": run_id,
            "workbench_search_id": "search",
            "caller": {
                "tenant_id": tenant,
                "user_id": account,
                "app_id": app_id,
                "user_from": "account",
                "invoke_from": "explore",
            },
            "dataset_ids": ["dataset"],
            "query": "同一个问题",
            "retrieval": {"mode": "multiple", "top_k": 1},
        }
    )
    hits: list[dict[str, str]] = [{"content": "完整片段" * 1000}] if retrieval_status == "returned" else []
    # The HTTP callback finishes before the worker consumes the Agent's start.
    retrieval_event(request, "running")
    retrieval_event(request, retrieval_status, results=hits)
    assert event_log.read_page(tenant, account, run_id)[0] == []
    for stage in ("started", "returned"):
        if stage == "returned" and consumer_event == "end":
            # A native failure can omit the return; the ordered consumer still
            # preserves the completed retrieval before closing this attempt.
            event_log.append_event(run_id, {"event": "workbench_end", "status": "failed"})
            break
        item = {
            "event": "workbench_activity",
            "backend_run_id": ticket,
            "source_event_id": stage,
            "data": {
                "kind": "tool",
                "tool_name": "knowledge_base_search",
                "stage": stage,
                "output": json.dumps({"search_id": "search", "status": retrieval_status}),
            },
        }
        event_log.append_event(run_id, item, expected_backend_run_id=ticket)
        if stage == "returned":
            event_log.append_event(run_id, item, expected_backend_run_id=ticket)
    saved = event_log.read_page(tenant, account, run_id)[0]
    assert [item["event"] for item in saved] == [
        "workbench_activity",
        "workbench_knowledge",
        "workbench_activity" if consumer_event == "tool" else "workbench_end",
    ]
    assert saved[0]["data"]["stage"] == "started"
    assert saved[1]["results"] == hits
    assert saved[1]["status"] == retrieval_status
    if consumer_event == "tool":
        assert saved[2]["data"]["stage"] == "returned"
    assert [item["_sequence"] for item in saved] == [1, 2, 3]
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        run.status = "completed"
        history = saved if consumer_event == "tool" else saved[:-1]
        assert event_log.history_events(run) == history
    assert list(event_log.stream_events(tenant, account, run_id))[:-1] == history


@pytest.mark.parametrize("protocol", [0, 1])
@pytest.mark.parametrize("tool_name", ["ask_human", "update_shared_environment"])
def test_pause_waits_for_the_ordered_consumer_before_closing_live_progress(
    journal: Journal,
    monkeypatch: pytest.MonkeyPatch,
    config_overrides: Callable[..., None],
    protocol: int,
    tool_name: str,
) -> None:
    from extensions.ext_redis import redis_client
    from services.workbench import runtime

    config_overrides(WORKBENCH_ENABLED=True)
    xadd = MagicMock()
    monkeypatch.setattr(redis_client, "xadd", xadd)
    monkeypatch.setattr(redis_client, "set", MagicMock())
    monkeypatch.setattr(redis_client, "xread", MagicMock(return_value=[]))
    factory, tenant, account, run_id, chat_id = journal
    conversation_id, binding_id = str(uuid4()), str(uuid4())
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        chat = session.get(WorkbenchChat, chat_id)
        assert run is not None
        assert chat is not None
        ticket = run.backend_run_id
        assert ticket is not None
        run.payload = json.dumps({"activity_protocol": protocol})
        chat.conversation_id = conversation_id
        session.add(
            AgentWorkspaceBinding(
                id=binding_id,
                tenant_id=tenant,
                app_id=chat.app_id,
                workspace_id=str(uuid4()),
                agent_id=chat.agent_id,
                agent_config_version_id=str(uuid4()),
                agent_config_version_kind=AgentConfigVersionKind.SNAPSHOT,
                backend_binding_ref="binding",
            )
        )
    terminal = AgentBackendDeferredToolCallInternalEvent(
        run_id=ticket,
        deferred_tool_call=DeferredToolCallPayload(tool_call_id="pause-call", tool_name=tool_name, args={}),
        session_snapshot=CompositorSessionSnapshot(layers=[]),
    )
    assert runtime.pause(tenant, conversation_id, account, terminal, binding_id)
    paused_status = "waiting_input" if tool_name == "ask_human" else "environment_update"
    if protocol == 0:
        assert event_log.read_state(tenant, account, run_id)["status"] == paused_status
        xadd.assert_called_once()
        return
    assert event_log.read_state(tenant, account, run_id)["status"] == "running"
    assert event_log.read_page(tenant, account, run_id)[0] == []
    xadd.assert_not_called()
    stream = event_log.stream_events(tenant, account, run_id)
    assert next(stream) is None
    # The producer has paused before the task drains these previously queued events.
    event_log.append_event(run_id, {"event": "workbench_activity", "data": {"kind": "tool", "stage": "returned"}})
    event_log.append_event(run_id, {"event": "message_end"})
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        saved = runtime.complete_pause(session, run, completed_stream=True)
        assert saved is not None
        assert saved["_sequence"] == 3
        assert run.status == paused_status
        assert "pending_pause" not in json.loads(run.payload)
        assert runtime.complete_pause(session, run, completed_stream=True) is None
    items = [next(stream), next(stream), next(stream)]
    assert [item["event"] for item in items if item is not None] == [
        "workbench_activity",
        "message_end",
        "workbench_status",
    ]
    last = next(stream)
    if protocol == 1 and tool_name == "ask_human":
        assert last is not None
        assert last["event"] == "workbench_end"
        assert last["status"] == paused_status
    else:
        assert last is None
    stream.close()


@pytest.mark.parametrize("status", ["completed", "waiting_input"])
def test_terminal_status_commits_with_remaining_knowledge_results(journal: Journal, status: str) -> None:
    factory, tenant, account, run_id, _ = journal
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        payload = json.loads(run.payload)
        payload["knowledge_events"] = [
            {
                "event": "workbench_knowledge",
                "status": "returned",
                "search_id": "last-search",
                "backend_run_id": run.backend_run_id,
                "source_event_id": "last-result",
                "results": [{"content": "最后片段"}],
            }
        ]
        run.payload = json.dumps(payload)
        run.status = status
        # The task commits its status and closing records under this same lock.
        if status == "waiting_input":
            event_log.append_locked(session, run, {"event": "workbench_status", "status": status})
        event_log.append_locked(session, run, {"event": "workbench_end", "status": status})
    items = list(event_log.stream_events(tenant, account, run_id))
    first, last = items[0], items[-1]
    assert first is not None
    assert last is not None
    assert first["event"] == "workbench_knowledge"
    assert first["results"] == [{"content": "最后片段"}]
    assert last["status"] == status
    assert len([item for item in items if item and item["event"] == "workbench_knowledge"]) == 1


@pytest.mark.parametrize("boundary", ["incomplete", "cancelled", "old-attempt"])
def test_pending_pause_cannot_override_failed_cancelled_or_newer_attempts(journal: Journal, boundary: str) -> None:
    from services.workbench import runtime

    factory, _, _, run_id, _ = journal
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        payload = json.loads(run.payload)
        payload["pending_pause"] = {
            "status": "waiting_input",
            "backend_run_id": "old" if boundary == "old-attempt" else run.backend_run_id,
        }
        run.payload = json.dumps(payload)
        if boundary == "cancelled":
            run.status = "cancelled"
        assert runtime.complete_pause(session, run, completed_stream=boundary != "incomplete") is None
        assert run.status == ("cancelled" if boundary == "cancelled" else "running")
        assert event_log.history_events(run) == []


def test_notification_failure_does_not_lose_committed_event(journal: Journal, monkeypatch: pytest.MonkeyPatch) -> None:
    _, tenant, account, run_id, _ = journal
    from unittest.mock import Mock

    redis = Mock()
    redis.xadd.side_effect = ConnectionError("offline")
    # Exercise the actual notification failure handler, not a successful fake.
    monkeypatch.setattr(event_log, "redis_client", redis)
    monkeypatch.setattr(event_log, "notify", _REAL_NOTIFY)
    assert event_log.append_event(run_id, {"event": "agent_message", "answer": "durable"}) == "1-0"
    assert event_log.read_page(tenant, account, run_id)[0][0]["answer"] == "durable"


@pytest.mark.parametrize("message_count", [1, 101])
def test_stop_closes_the_journal_before_late_frames_and_cleanup(
    journal: Journal, monkeypatch: pytest.MonkeyPatch, message_count: int
) -> None:
    from flask import Flask

    from controllers.console import workbench
    from tasks import workbench_tasks

    factory, tenant, account, run_id, _ = journal
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        for index in range(message_count):
            event_log.append_locked(session, run, {"event": "agent_message", "answer": str(index)})
        payload = json.loads(run.payload)
        payload["knowledge_events"] = [
            {
                "event": "workbench_knowledge",
                "status": "returned",
                "search_id": "known",
                "backend_run_id": run.backend_run_id,
                "source_event_id": "known-result",
                "results": [{"content": "取消前已返回的完整结果"}],
            }
        ]
        run.payload = json.dumps(payload)
    monkeypatch.setattr(workbench.WorkbenchResource, "owner", lambda _self: (tenant, account))
    monkeypatch.setattr(workbench_tasks.force_stop, "delay", MagicMock())
    monkeypatch.setattr(workbench_tasks, "stop_native", MagicMock())
    monkeypatch.setattr(workbench_tasks, "fence_remote", lambda _ticket: True)
    monkeypatch.setattr(workbench_tasks.scheduler, "release", MagicMock())
    monkeypatch.setattr(workbench_tasks.reconcile, "delay", MagicMock())
    with Flask(__name__).test_request_context():
        workbench.Stop().post(UUID(run_id))
    saved, status, _ = event_log.read_page(tenant, account, run_id)
    assert status == "cancelled"
    live = list(event_log.stream_events(tenant, account, run_id))
    assert live[-2] is not None
    assert live[-2]["event"] == "workbench_knowledge"
    assert live[-2]["results"] == [{"content": "取消前已返回的完整结果"}]
    assert live[-1] is not None
    assert live[-1]["status"] == "cancelled"
    for name in (
        "agent_message",
        "message_end",
        "error",
        "workbench_status",
        "workbench_activity",
        "workbench_context",
        "workbench_knowledge",
        "workbench_end",
    ):
        assert event_log.append_event(run_id, {"event": name, "status": "cancelled", "answer": "late"}) is None
    # The asynchronous cancellation worker must not flush another result or end.
    workbench_tasks.force_stop.run(run_id, account)
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        assert json.loads(run.payload)["activity_closed"] is True
        assert event_log.append_locked(session, run, {"event": "workbench_end", "status": "cancelled"}) is None
        assert event_log.history_events(run) == live[:-1]
    assert event_log.read_page(tenant, account, run_id)[0] == saved


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_terminal_boundary_rejects_every_later_record(journal: Journal, status: str) -> None:
    factory, _, _, run_id, _ = journal
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        run.status = status
        event_log.append_locked(session, run, {"event": "workbench_end", "status": status})
        assert event_log.append_locked(session, run, {"event": "agent_message", "answer": "late"}) is None
        assert event_log.append_locked(session, run, {"event": "workbench_end", "status": status}) is None


def test_legacy_stop_retains_the_existing_event_protocol(journal: Journal, monkeypatch: pytest.MonkeyPatch) -> None:
    from flask import Flask

    from controllers.console import workbench
    from tasks import workbench_tasks

    factory, tenant, account, run_id, _ = journal
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        run.payload = json.dumps({"activity_protocol": 0})
    monkeypatch.setattr(workbench.WorkbenchResource, "owner", lambda _self: (tenant, account))
    monkeypatch.setattr(workbench_tasks.force_stop, "delay", MagicMock())
    with Flask(__name__).test_request_context():
        workbench.Stop().post(UUID(run_id))
    with factory() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        assert run.status == "cancelled"
        assert "activity_closed" not in json.loads(run.payload)
        assert event_log.history_events(run) == []
