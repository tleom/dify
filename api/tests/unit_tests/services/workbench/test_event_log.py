"""SQLite exercises persisted event order/ownership, with Redis failure isolated at the wake-up boundary."""

import json
from collections.abc import Callable, Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.exceptions import NotFound

from core.db import session_factory as factory_module
from models.base import TypeBase
from models.workbench import WorkbenchChat, WorkbenchRun, WorkbenchRunEvent
from services.workbench import event_log

_REAL_NOTIFY = event_log.notify
type Journal = tuple[sessionmaker[Session], str, str, str, str]


@pytest.fixture
def journal(monkeypatch: pytest.MonkeyPatch) -> Iterator[Journal]:
    engine = create_engine("sqlite://")
    TypeBase.metadata.create_all(engine, tables=[TypeBase.metadata.tables[model.__tablename__] for model in (
        WorkbenchChat, WorkbenchRun, WorkbenchRunEvent,
    )])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(factory_module, "_session_maker", factory)
    monkeypatch.setattr(event_log, "notify", lambda *_: None)
    tenant, account, run_id, chat_id = (str(uuid4()) for _ in range(4))
    with factory.begin() as session:
        session.add(WorkbenchChat(
            id=chat_id, tenant_id=tenant, account_id=account, agent_id=str(uuid4()), app_id=str(uuid4()),
            base_snapshot_id=str(uuid4()),
        ))
        session.add(WorkbenchRun(
            id=run_id, chat_id=chat_id, tenant_id=tenant, account_id=account, revision_id=str(uuid4()),
            request_key="request", payload=json.dumps({"activity_protocol": 1, "pending": {"tool_call_id": "human"}}),
            status="running", backend_run_id=str(uuid4()),
        ))
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
        "event": "workbench_activity", "backend_run_id": ticket, "source_event_id": "1-0", "data": {},
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
            setattr(chat, {"chat_tenant": "tenant_id", "chat_account": "account_id", "deleted": "deleted"}[boundary],
                    1 if boundary == "deleted" else str(uuid4()))
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
        payload["effective_soul"] = {"knowledge": {"sets": [{
            "id": "kb", "name": "知识库", "datasets": [{"id": "dataset"}],
            "query": {"mode": "generated_query"},
        }]}}
        run.payload = json.dumps(payload)
    request = InnerKnowledgeRetrieveRequest.model_validate({
        "workbench_run_id": run_id, "workbench_search_id": "search",
        "caller": {"tenant_id": tenant, "user_id": account, "app_id": app_id,
                   "user_from": "account", "invoke_from": "explore"},
        "dataset_ids": ["dataset"], "query": "同一个问题", "retrieval": {"mode": "multiple", "top_k": 1},
    })
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
            "event": "workbench_activity", "backend_run_id": ticket, "source_event_id": stage,
            "data": {"kind": "tool", "tool_name": "knowledge_base_search", "stage": stage,
                     "output": json.dumps({"search_id": "search", "status": retrieval_status})},
        }
        event_log.append_event(run_id, item, expected_backend_run_id=ticket)
        if stage == "returned":
            event_log.append_event(run_id, item, expected_backend_run_id=ticket)
    saved = event_log.read_page(tenant, account, run_id)[0]
    assert [item["event"] for item in saved] == [
        "workbench_activity", "workbench_knowledge",
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
