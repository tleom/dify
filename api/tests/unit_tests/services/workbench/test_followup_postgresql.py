"""Opt-in row-lock proof against an isolated PostgreSQL fixture.

WORKBENCH_TEST_POSTGRES_URL must point to a disposable test server. Each test
creates its own schema; no existing application schema or data is changed.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from models.workbench import WorkbenchChat, WorkbenchRun
from services.workbench import context_status, followups

from . import test_followup_recovery, test_followups
from .test_context_status import event
from .test_followups import Queue

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKBENCH_TEST_POSTGRES_URL"), reason="requires an isolated PostgreSQL test fixture"
)


@pytest.fixture
def pg_queue(monkeypatch: pytest.MonkeyPatch, config_overrides: Callable[..., None], tmp_path: Path) -> Iterator[Queue]:
    schema = "followup_" + uuid4().hex
    url = os.environ["WORKBENCH_TEST_POSTGRES_URL"]
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}", "application_name": schema})
    monkeypatch.setattr(test_followups, "create_engine", lambda *_: engine)
    fixture = test_followups.queue_fixture(monkeypatch, config_overrides, tmp_path)
    queue = next(fixture)
    queue.admin, queue.application_name = admin, schema
    yield queue
    next(fixture, None)
    admin.dispose()


@pytest.mark.parametrize("change", ["steer", "seal"])
def test_context_lock_blocks_concurrent_payload_writer_without_losing_fields(
    pg_queue: Queue, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    queue = pg_queue
    original = queue.send("原任务")
    queue.running(original["id"])
    pending = queue.send("并发补充")
    conversation_id = str(uuid4())
    with queue.factory.begin() as session:
        run = session.get(WorkbenchRun, original["id"])
        assert run is not None
        stored_row = session.get(WorkbenchChat, run.chat_id)
        assert stored_row is not None
        stored_row.conversation_id = conversation_id
        data = json.loads(run.payload)
        data["effective_soul"] = {"model": {"model_provider": "test", "model": "test"}}
        run.payload = json.dumps(data)
        message = session.get(WorkbenchRun, pending["id"])
        assert message is not None
        message_payload = json.loads(message.payload)
        message_payload["effective_soul"] = data["effective_soul"]
        message.payload = json.dumps(message_payload)
    assert queue.admin is not None
    entered, release = threading.Event(), threading.Event()
    current_run = context_status.current_run

    def hold_row(
        session: Session, tenant_id: str, conversation_id: str, account_id: str, *, for_update: bool = False
    ) -> WorkbenchRun | None:
        run = current_run(session, tenant_id, conversation_id, account_id, for_update=for_update)
        entered.set()
        assert release.wait(10), "test did not release its owned row lock"
        return run

    monkeypatch.setattr(context_status, "current_run", hold_row)
    monkeypatch.setattr(
        context_status, "redis_client", SimpleNamespace(xadd=lambda *_: b"100-0", expire=lambda *_: None)
    )
    update = event().model_copy(update={"run_id": queue.get(original["id"]).backend_run_id})
    with ThreadPoolExecutor(max_workers=2) as executor:
        recording = executor.submit(
            context_status.record_context_status, queue.owner[0], conversation_id, queue.owner[1], update
        )
        assert entered.wait(5)
        writing = executor.submit(
            lambda: (
                followups.steer(*queue.owner, pending["id"], original["id"])
                if change == "steer"
                else queue.poll(original["id"], action="seal")
            )
        )
        blocked = False
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with queue.admin.connect() as connection:
                    blocked = bool(
                        connection.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE application_name=:name AND wait_event_type='Lock' AND state='active'"
                            ),
                            {"name": queue.application_name},
                        )
                    )
                if blocked:
                    break
                time.sleep(0.02)
            assert blocked, "PostgreSQL must report an actual lock wait"
            assert not writing.done()
        finally:
            release.set()
        recording.result(timeout=5)
        writing.result(timeout=5)
    data = json.loads(queue.get(original["id"]).payload)
    assert data["context_usage"]["used_tokens"] == 500
    if change == "steer":
        assert [item["id"] for item in data["steering_messages"]] == [pending["id"]]
    else:
        assert data["steering_closed_ticket"] == queue.get(original["id"]).backend_run_id


@pytest.mark.parametrize("query", ["", "补充当前任务"])
def test_paused_queue_continuation_on_postgresql(pg_queue: Queue, query: str) -> None:
    test_followups.test_paused_queue_waits_and_manual_continuation_precedes_all_three_messages(pg_queue, query)


def test_recovery_ignores_historical_pause_and_keeps_bounded_batches_on_postgresql(pg_queue: Queue) -> None:
    from .test_followup_edges import test_recovery_filters_fifty_paused_heads_before_limiting_the_batch

    test_recovery_filters_fifty_paused_heads_before_limiting_the_batch(pg_queue)


@pytest.mark.parametrize("phase", ["idle", "running", "paused"])
def test_attachment_only_with_knowledge_on_postgresql(
    pg_queue: Queue, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    from .test_followup_edges import test_attachment_only_send_with_persistent_knowledge_selection

    test_attachment_only_send_with_persistent_knowledge_selection(pg_queue, monkeypatch, phase)


def test_continue_uses_original_revision_on_postgresql(pg_queue: Queue, monkeypatch: pytest.MonkeyPatch) -> None:
    from .test_followup_edges import test_blank_continue_uses_owned_original_revision_despite_current_config_changes

    test_blank_continue_uses_owned_original_revision_despite_current_config_changes(pg_queue, monkeypatch)


@pytest.mark.parametrize("ending", ["failed", "interrupted"])
@pytest.mark.parametrize("queue_after_failure", [False, True])
def test_automatic_recovery_keeps_queue_order_on_postgresql(
    pg_queue: Queue, monkeypatch: pytest.MonkeyPatch, ending: str, queue_after_failure: bool
) -> None:
    test_followup_recovery.configure_local_control(monkeypatch)
    test_followup_recovery.test_automatic_continuation_precedes_all_three_waiting_messages(
        pg_queue, ending, queue_after_failure
    )


@pytest.mark.parametrize("create_successor", [False, True])
def test_pause_and_continue_during_recovery_on_postgresql(
    pg_queue: Queue, monkeypatch: pytest.MonkeyPatch, create_successor: bool
) -> None:
    test_followup_recovery.configure_local_control(monkeypatch)
    test_followup_recovery.test_pause_cancels_recovery_and_keeps_queue_until_blank_continue(pg_queue, create_successor)


def test_steering_during_recovery_on_postgresql(pg_queue: Queue, monkeypatch: pytest.MonkeyPatch) -> None:
    test_followup_recovery.configure_local_control(monkeypatch)
    test_followup_recovery.test_steering_during_recovery_is_kept_in_the_successors_context(pg_queue)


@pytest.mark.parametrize("continuation", ["automatic", "manual"])
@pytest.mark.parametrize("ending", ["failed", "interrupted"])
def test_newer_ancestor_does_not_supersede_waiting_task_on_postgresql(
    pg_queue: Queue, monkeypatch: pytest.MonkeyPatch, continuation: str, ending: str
) -> None:
    test_followup_recovery.configure_local_control(monkeypatch)
    test_followup_recovery.test_waiting_task_retains_its_own_recovery_after_a_newer_ancestor(
        pg_queue, continuation, ending
    )


@pytest.mark.parametrize("ending", ["failed", "cancelled", "interrupted"])
def test_cleanup_proof_tracks_the_current_ticket_on_postgresql(pg_queue: Queue, ending: str) -> None:
    from sqlalchemy import select

    from services.workbench import recovery

    item = pg_queue.send("cleanup proof")
    pg_queue.running(item["id"])
    with pg_queue.factory.begin() as session:
        run = session.get(WorkbenchRun, item["id"])
        assert run is not None
        run.status = ending
        ticket = run.backend_run_id
    with pg_queue.factory() as session:
        assert session.scalar(select(WorkbenchRun.id).where(recovery.cleanup_pending_condition())) == item["id"]
    followups.save_fenced_state(ticket, {"history": None})
    with pg_queue.factory.begin() as session:
        assert session.scalar(select(WorkbenchRun.id).where(recovery.cleanup_pending_condition())) is None
        stored_row = session.get(WorkbenchRun, item["id"])
        assert stored_row is not None
        stored_row.backend_run_id = str(uuid4())
    followups.save_fenced_state(ticket, {"history": None})
    with pg_queue.factory() as session:
        assert session.scalar(select(WorkbenchRun.id).where(recovery.cleanup_pending_condition())) == item["id"]
