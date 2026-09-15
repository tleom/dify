"""Exercise durable recovery intent with real transactions and no remote execution."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import TypedDict, cast, override
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.exceptions import Conflict, NotFound

from core.db import session_factory as factory_module
from models.base import TypeBase
from models.workbench import WorkbenchChat, WorkbenchRun, WorkbenchRunEvent
from services.workbench import recovery, service

type RecoveryDatabase = tuple[sessionmaker[Session], str, str, str, str, Mock, Mock]


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[RecoveryDatabase]:
    engine = create_engine("sqlite://")
    TypeBase.metadata.create_all(
        engine,
        tables=[
            TypeBase.metadata.tables[model.__tablename__] for model in (WorkbenchChat, WorkbenchRun, WorkbenchRunEvent)
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(factory_module, "_session_maker", factory)
    monkeypatch.setattr(service, "authorize", lambda *_: None)
    monkeypatch.setattr(recovery, "notify", lambda *_: None)
    publish = Mock()
    monkeypatch.setattr(recovery.scheduler, "publish", publish)
    monkeypatch.setattr(recovery.scheduler, "release", Mock())
    from tasks import workbench_tasks

    fence = Mock(return_value=True)
    fence.real_function = workbench_tasks.fence_remote
    monkeypatch.setattr(workbench_tasks, "fence_remote", fence)
    tenant, account, chat_id, run_id = (str(uuid4()) for _ in range(4))
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
                tenant_id=tenant,
                account_id=account,
                chat_id=chat_id,
                revision_id=str(uuid4()),
                request_key="original",
                status="running",
                event_log="[]",
                backend_run_id=str(uuid4()),
                payload=json.dumps(
                    {
                        "query": "完成长报告",
                        "recovery": {"attempt": 0},
                        "activity_protocol": 1,
                        "effective_soul": {"model": "frozen-model"},
                        "version": 3,
                        "sandbox_paths": ["conversations/current/draft.docx"],
                        "output_history": {"messages": []},
                    }
                ),
            )
        )
    yield factory, tenant, account, chat_id, run_id, publish, fence
    engine.dispose()


def fail(factory: sessionmaker[Session], run_id: str) -> bool:
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        run.status, run.error = "failed", "工具连续调用失败 5 次"
        marked = recovery.mark_failure(run)
        payload = json.loads(run.payload)
        if marked:
            payload["recovery"]["due_at"] = 1
            run.payload = json.dumps(payload)
        return marked


@pytest.mark.parametrize(
    ("frames", "expected_status", "should_recover"),
    [
        ([{"event": "message", "answer": "partial"}], "failed", True),
        ([{"event": "message_end"}], "completed", False),
        ([{"event": "error", "message": "provider disconnected"}], "failed", True),
    ],
)
def test_executor_requires_terminal_frame(
    database: RecoveryDatabase,
    monkeypatch: pytest.MonkeyPatch,
    frames: list[dict[str, str]],
    expected_status: str,
    should_recover: bool,
) -> None:
    from flask import Flask
    from sqlalchemy.orm import Session

    from tasks import workbench_tasks as tasks

    factory, tenant, account, _, run_id, _, fence = database
    with factory.begin() as session:
        stored_row = session.get(WorkbenchRun, run_id)
        assert stored_row is not None
        stored_row.status = "queued"
    original_get = Session.get
    user, app_model = Mock(), Mock(tenant_id=tenant)

    def get(session: Session, entity: type[object], ident: str) -> object:
        if entity is tasks.Account:
            return user
        if entity is tasks.App:
            return app_model
        return original_get(session, entity, ident)

    monkeypatch.setattr(Session, "get", get)
    monkeypatch.setattr(tasks, "authorize", Mock())
    monkeypatch.setattr(tasks, "event", Mock(return_value=None))
    monkeypatch.setattr(tasks, "notify", Mock())
    monkeypatch.setattr(tasks.threading, "Thread", Mock())
    monkeypatch.setattr(tasks.scheduler, "heartbeat", Mock(return_value=True))
    monkeypatch.setattr(tasks, "AgentAppGenerator", Mock(return_value=Mock(generate=Mock(return_value=iter(frames)))))
    wake_recovery, dispatch = Mock(), Mock()
    monkeypatch.setattr(tasks.recover_run, "apply_async", wake_recovery)
    wake_followups = Mock()
    monkeypatch.setattr(tasks.advance_followups, "delay", wake_followups)
    monkeypatch.setattr(tasks.dispatch, "delay", dispatch)
    with Flask(__name__).app_context():
        tasks.execute(f"{tenant}:{account}", run_id)
    with factory() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        assert run.status == expected_status
        state = recovery.recovery_dto(json.loads(run.payload))
        assert state is not None
        assert state["pending"] is should_recover
        if frames[0]["event"] == "message":
            assert run.error is not None
            assert "事件流提前结束" in run.error
    assert wake_recovery.called is should_recover
    assert wake_followups.called is (expected_status == "completed")
    assert fence.called is should_recover
    dispatch.assert_called_once()


class HumanInputState(TypedDict):
    request_id: str
    deadline_at: float


def ask(factory: sessionmaker[Session], run_id: str) -> HumanInputState:
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        run.status = "waiting_input"
        payload = json.loads(run.payload)
        payload["pending"] = {
            "tool_name": "ask_human",
            "tool_call_id": "human-call",
            "args": {"question": "请补充"},
        }
        run.payload = json.dumps(payload)
        recovery.mark_input_wait(run)
        return cast(HumanInputState, json.loads(run.payload)["human_input"])


def test_three_automatic_continuations_preserve_goal_and_frozen_configuration(database: RecoveryDatabase) -> None:
    factory, _, _, _, source_id, publish, _ = database
    root_id = source_id
    for attempt in range(1, 4):
        assert fail(factory, source_id)
        child_id = recovery.continue_failed(source_id)
        assert child_id
        assert child_id != source_id
        assert recovery.continue_failed(source_id) == child_id
        with factory() as session:
            source = session.get(WorkbenchRun, source_id)
            assert source is not None
            child = session.get(WorkbenchRun, child_id)
            assert child is not None
            payload = json.loads(child.payload)
            assert child.status == "queued"
            assert payload["query"] == "继续"
            assert payload["recovery"]["attempt"] == attempt
            assert payload["recovery"]["root_run_id"] == root_id
            assert payload["recovery"]["goal"] == "完成长报告"
            assert payload["effective_soul"] == {"model": "frozen-model"}
            assert payload["branch_parent_run_id"] == source_id
            assert child.revision_id == source.revision_id
            assert payload["sandbox_paths"] == ["conversations/current/draft.docx"]
            state = recovery.recovery_dto(json.loads(source.payload))
            assert state is not None
            assert not state["pending"]
        source_id = child_id
    assert not fail(factory, source_id)
    assert recovery.continue_failed(source_id) is None
    assert publish.call_count == 3
    with factory() as session:
        assert len(list(session.scalars(select(WorkbenchRun)))) == 4


def test_uncertain_remote_execution_must_be_fenced_before_continuation(database: RecoveryDatabase) -> None:
    factory, _, _, _, run_id, publish, fence = database
    fail(factory, run_id)
    fence.return_value = False
    assert recovery.continue_failed(run_id) is None
    publish.assert_not_called()
    fence.return_value = True
    assert recovery.continue_failed(run_id)
    publish.assert_called_once()


@pytest.mark.parametrize("status", ["running", "cancelled"])
def test_remote_fence_stores_wire_checkpoint_only_after_execution_stops(
    database: RecoveryDatabase, monkeypatch: pytest.MonkeyPatch, config_overrides: Callable[..., None], status: str
) -> None:
    import httpx

    from services.workbench import followups
    from tasks import workbench_tasks

    factory, _, _, _, run_id, _, fence = database
    fail(factory, run_id)
    with factory() as session:
        stored_row = session.get(WorkbenchRun, run_id)
        assert stored_row is not None
        ticket = stored_row.backend_run_id
    config_overrides(AGENT_BACKEND_BASE_URL="http://agent.test", AGENT_BACKEND_API_TOKEN="test-token")
    actual_client = httpx.Client
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"run_id": ticket, "status": status, "history": {"messages": []}})

    monkeypatch.setattr(
        workbench_tasks.httpx,
        "Client",
        lambda **kwargs: actual_client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    save = Mock(wraps=followups.save_fenced_state)
    monkeypatch.setattr(followups, "save_fenced_state", save)
    assert fence.real_function(ticket) is (status != "running")
    assert save.call_count == int(status != "running")
    assert requests[0].url.path == f"/runs/{ticket}/fence"
    assert requests[0].method == "POST"
    with factory() as session:
        stored_row = session.get(WorkbenchRun, run_id)
        assert stored_row is not None
        assert json.loads(stored_row.payload)["output_history"] == {"messages": []}
        stored_row = session.get(WorkbenchRun, run_id)
        assert stored_row is not None
        confirmed = json.loads(stored_row.payload).get("cleanup_confirmed_ticket")
        assert (confirmed == ticket) is (status != "running")


def test_due_scan_rotates_past_fifty_blocked_recoveries(database: RecoveryDatabase) -> None:
    from datetime import datetime, timedelta

    factory, tenant, account, chat_id, run_id, _, _ = database
    fail(factory, run_id)
    last_id = str(uuid4())
    with factory.begin() as session:
        stored_row = session.get(WorkbenchRun, run_id)
        assert stored_row is not None
        stored_row.updated_at = datetime(2020, 1, 1)
        for index in range(50):
            session.add(
                WorkbenchRun(
                    id=last_id if index == 49 else str(uuid4()),
                    tenant_id=tenant,
                    account_id=account,
                    chat_id=chat_id,
                    revision_id=str(uuid4()),
                    request_key=f"blocked-{index}",
                    status="failed",
                    event_log="[]",
                    payload=json.dumps({"recovery": {"attempt": 0, "due_at": 1}}),
                    updated_at=datetime(2020, 1, 1) + timedelta(seconds=index + 1),
                )
            )
    first, _ = recovery.due_runs()
    assert len(first) == 50
    assert last_id not in first
    # None of the first batch can resume. The next scan must still reach the
    # remaining task instead of repeatedly selecting those same fifty rows.
    second, _ = recovery.due_runs()
    assert second[0] == last_id


def test_fenced_checkpoint_is_restored_before_the_successor_reads_history(database: RecoveryDatabase) -> None:
    from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart

    from services.workbench.branches import output_history

    factory, _, _, _, run_id, _, _ = database
    fail(factory, run_id)
    state = PydanticAIHistoryRuntimeState(
        messages=[
            ModelRequest(parts=[UserPromptPart("完成长报告")]),
            ModelResponse(parts=[ToolCallPart("external_write", {"value": "known"}, "uncertain-call")]),
        ]
    ).model_dump(mode="json")
    with factory() as session:
        stored_row = session.get(WorkbenchRun, run_id)
        assert stored_row is not None
        ticket = stored_row.backend_run_id
    recovery.save_fenced_history(ticket, state)
    child_id = recovery.continue_failed(run_id)
    with factory() as session:
        source = session.get(WorkbenchRun, run_id)
        assert source is not None
        child = session.get(WorkbenchRun, child_id)
        assert child is not None
        assert json.loads(child.payload)["branch_parent_run_id"] == source.id
        assert output_history(session, source) == state
    # A later execution has a different ticket. Its checkpoint cannot be overwritten.
    with factory.begin() as session:
        stored_row = session.get(WorkbenchRun, run_id)
        assert stored_row is not None
        stored_row.backend_run_id = str(uuid4())
    recovery.save_fenced_history(ticket, {"messages": []})
    with factory() as session:
        assert output_history(session, session.get(WorkbenchRun, run_id)) == state


@pytest.mark.parametrize("already_queued", [False, True])
def test_pause_stops_pending_or_already_queued_automatic_successor(
    database: RecoveryDatabase, already_queued: bool
) -> None:
    factory, tenant, account, _, run_id, publish, _ = database
    fail(factory, run_id)
    child_id = recovery.continue_failed(run_id) if already_queued else None
    targets = recovery.cancel_chain(tenant, account, run_id)
    assert set(targets) == ({run_id, child_id} if child_id else {run_id})
    with factory() as session:
        for target in targets:
            run = session.get(WorkbenchRun, target)
            assert run is not None
            assert run.status == "cancelled"
            state = recovery.recovery_dto(json.loads(run.payload))
            assert state is not None
            assert not state["pending"]
    assert not fail(factory, child_id or run_id)
    recovery.continue_failed(child_id or run_id)
    assert publish.call_count == int(already_queued)


def test_manual_message_supersedes_pending_automatic_continuation(database: RecoveryDatabase) -> None:
    factory, tenant, account, chat_id, run_id, publish, _ = database
    fail(factory, run_id)
    with factory.begin() as session:
        session.add(
            WorkbenchRun(
                id=str(uuid4()),
                tenant_id=tenant,
                account_id=account,
                chat_id=chat_id,
                revision_id=str(uuid4()),
                request_key="manual",
                status="queued",
                payload='{"query":"改要求"}',
            )
        )
    assert recovery.continue_failed(run_id) is None
    publish.assert_not_called()
    with factory() as session:
        stored_row = session.get(WorkbenchRun, run_id)
        assert stored_row is not None
        assert json.loads(stored_row.payload)["recovery"]["cancelled"]


def test_untouched_input_deadline_resumes_once_and_preserves_auto_attempts(
    database: RecoveryDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, tenant, account, _, run_id, publish, _ = database
    monkeypatch.setattr(recovery.time, "time", lambda: 1000)
    state = ask(factory, run_id)
    assert state["deadline_at"] == 1060
    assert not recovery.expire_input(run_id)
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        payload = json.loads(run.payload)
        payload["recovery"]["attempt"] = 2
        run.payload = json.dumps(payload)
    monkeypatch.setattr(recovery.time, "time", lambda: 1060)
    assert not recovery.expire_input(run_id, owner=(tenant, account), request_id="old-question")
    assert recovery.expire_input(run_id, owner=(tenant, account), request_id=state["request_id"])
    assert not recovery.expire_input(run_id)
    publish.assert_called_once_with(tenant, account, run_id)
    with factory() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        payload = json.loads(run.payload)
        assert run.status == "queued"
        assert run.backend_run_id is None
        assert payload["recovery"]["attempt"] == 2
        result = payload["continuation"]["calls"]["human-call"]
        assert result["status"] == "timeout"
        assert result["values"] == {}
        assert result["action"] is None
        assert "human_input" not in payload
        assert "pending" not in payload
    with pytest.raises(Conflict):
        recovery.interact(tenant, account, run_id, state["request_id"])


def test_any_interaction_cancels_timer_but_explicit_skip_still_works(
    database: RecoveryDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, tenant, account, _, run_id, publish, _ = database
    monkeypatch.setattr(recovery.time, "time", lambda: 1000)
    state = ask(factory, run_id)
    dto = recovery.interact(tenant, account, run_id, state["request_id"])
    assert dto["human_input"]["interacted"]
    assert dto["human_input"]["deadline_at"] is None
    monkeypatch.setattr(recovery.time, "time", lambda: 10000)
    assert not recovery.expire_input(run_id)
    publish.assert_not_called()
    assert recovery.expire_input(run_id, owner=(tenant, account), request_id=state["request_id"], manual=True)
    with factory() as session:
        stored_row = session.get(WorkbenchRun, run_id)
        assert stored_row is not None
        result = json.loads(stored_row.payload)["continuation"]["calls"]["human-call"]
        assert result["status"] == "cancelled"
        assert "跳过" in result["message"]
        assert result["values"] == {}


def test_old_questions_have_no_automatic_deadline_and_can_be_skipped_explicitly(database: RecoveryDatabase) -> None:
    factory, tenant, account, _, run_id, _, _ = database
    ask(factory, run_id)
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        payload = json.loads(run.payload)
        payload.pop("human_input")
        run.payload = json.dumps(payload)
    assert not recovery.expire_input(run_id)
    assert recovery.expire_input(run_id, owner=(tenant, account), request_id="human-call", manual=True)


def test_input_actions_enforce_owner_and_current_request(database: RecoveryDatabase) -> None:
    factory, tenant, account, _, run_id, publish, _ = database
    state = ask(factory, run_id)
    with pytest.raises(NotFound):
        recovery.interact(tenant, str(uuid4()), run_id, state["request_id"])
    with pytest.raises(NotFound):
        recovery.expire_input(run_id, owner=(tenant, str(uuid4())), request_id=state["request_id"], manual=True)
    with pytest.raises(Conflict):
        recovery.interact(tenant, account, run_id, "stale")
    assert not recovery.expire_input(run_id, owner=(tenant, account), request_id="stale", manual=True)
    publish.assert_not_called()


def test_restart_scan_recovers_only_due_intent_and_reports_active_chat(
    database: RecoveryDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, tenant, account, chat_id, run_id, _, _ = database
    monkeypatch.setattr(recovery.time, "time", lambda: 1000)
    fail(factory, run_id)
    assert recovery.due_runs() == ([run_id], [])
    with factory() as session:
        assert (
            session.scalar(
                select(service._chat_has_run(tenant, account, service.ACTIVE_STATUSES)).where(
                    WorkbenchChat.id == chat_id,
                )
            )
            is True
        )
    recovery.cancel_chain(tenant, account, run_id)
    assert recovery.due_runs() == ([], [])
    with factory() as session:
        assert (
            session.scalar(
                select(service._chat_has_run(tenant, account, service.ACTIVE_STATUSES)).where(
                    WorkbenchChat.id == chat_id,
                )
            )
            is False
        )
    state = ask(factory, run_id)
    assert recovery.due_runs() == ([], [])
    monkeypatch.setattr(recovery.time, "time", lambda: 1061)
    assert recovery.due_runs() == ([], [run_id])
    recovery.interact(tenant, account, run_id, state["request_id"])
    assert recovery.due_runs() == ([], [])


def test_terminal_stream_and_typed_response_expose_recovery_and_countdown(database: RecoveryDatabase) -> None:
    from controllers.console.workbench import WorkbenchRunResponse
    from services.workbench.event_log import stream_events

    factory, tenant, account, _, run_id, _, _ = database
    ask(factory, run_id)
    with factory() as session:
        dto = WorkbenchRunResponse.model_validate(service.run_dto(session.get(WorkbenchRun, run_id)))
        assert dto.human_input is not None
        assert dto.human_input.deadline_at is not None
        assert dto.human_input.deadline_at > dto.human_input.server_now
    fail(factory, run_id)
    event = list(stream_events(tenant, account, run_id))[-1]
    assert event is not None
    state = event["recovery"]
    assert isinstance(state, dict)
    assert state["pending"] is True


@pytest.mark.parametrize("lease_renewed", [False, True])
def test_reconcile_recovers_lost_redis_lease_but_preserves_a_fresh_lease(
    database: RecoveryDatabase,
    monkeypatch: pytest.MonkeyPatch,
    config_overrides: Callable[..., None],
    lease_renewed: bool,
) -> None:
    from tasks import workbench_tasks

    factory, _, _, _, run_id, _, fence = database
    config_overrides(WORKBENCH_ENABLED=True)
    monkeypatch.setattr(workbench_tasks.redis_client, "zrangebyscore", lambda *_: [])
    monkeypatch.setattr(workbench_tasks.redis_client, "zrange", lambda *_: [])
    monkeypatch.setattr(
        workbench_tasks.redis_client, "zscore", lambda *_: recovery.time.time() + 80 if lease_renewed else None
    )
    monkeypatch.setattr(workbench_tasks.redis_client, "get", Mock(side_effect=RuntimeError("stop channel unavailable")))
    monkeypatch.setattr(workbench_tasks.maintenance, "gated_owners", lambda: set())
    monkeypatch.setattr(workbench_tasks.dispatch, "delay", Mock())
    monkeypatch.setattr(workbench_tasks.recover_run, "delay", Mock())
    workbench_tasks.reconcile.run()
    with factory() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        assert run.status == ("running" if lease_renewed else "interrupted")
        state = recovery.recovery_dto(json.loads(run.payload))
        assert state is not None
        assert state["pending"] == (not lease_renewed)
    assert fence.call_count == int(not lease_renewed)


def test_slow_lease_reply_does_not_revoke_a_lease_that_was_fresh_when_queried(
    database: RecoveryDatabase, monkeypatch: pytest.MonkeyPatch, config_overrides: Callable[..., None]
) -> None:
    from tasks import workbench_tasks

    from .test_cleanup_recovery import stub_reconcile

    factory, _, _, _, run_id, _, fence = database
    config_overrides(WORKBENCH_ENABLED=True)
    stub_reconcile(monkeypatch)
    clock = {"now": 1000.0}
    monkeypatch.setattr(workbench_tasks.time, "time", lambda: clock["now"])

    def delayed_lease(*_: object) -> float:
        clock["now"] = 1120.0
        return 1080.0

    monkeypatch.setattr(workbench_tasks.redis_client, "zscore", delayed_lease)
    workbench_tasks.reconcile.run()
    with factory() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        assert run.status == "running"
    fence.assert_not_called()


def test_fence_accepts_a_real_cleanup_response_after_ten_seconds(
    database: RecoveryDatabase, config_overrides: Callable[..., None]
) -> None:
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    factory, _, _, _, run_id, _, fence = database
    fail(factory, run_id)
    with factory() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        ticket = run.backend_run_id
    assert ticket is not None
    response = json.dumps({"run_id": ticket, "status": "cancelled", "history": {"messages": []}}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            time.sleep(10.5)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    config_overrides(
        AGENT_BACKEND_BASE_URL=f"http://127.0.0.1:{server.server_port}", AGENT_BACKEND_API_TOKEN="test-token"
    )
    try:
        assert fence.real_function(ticket)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    with factory() as session:
        run = session.get(WorkbenchRun, run_id)
        assert run is not None
        assert json.loads(run.payload)["cleanup_confirmed_ticket"] == ticket
