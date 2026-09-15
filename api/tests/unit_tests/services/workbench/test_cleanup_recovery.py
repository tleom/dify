"""SQL cleanup proof survives loss of the ephemeral scheduler reservation."""

import json
from datetime import datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.workbench import WorkbenchRun
from services.workbench import followups, recovery

from . import test_recovery

database = test_recovery.database


def stub_executor(monkeypatch, tenant):
    from tasks import workbench_tasks as tasks

    original_get = Session.get

    def get(session, entity, ident, **kwargs):
        if entity is tasks.Account:
            return Mock()
        if entity is tasks.App:
            return Mock(tenant_id=tenant)
        return original_get(session, entity, ident, **kwargs)

    monkeypatch.setattr(Session, "get", get)
    monkeypatch.setattr(tasks, "authorize", Mock())
    monkeypatch.setattr(tasks, "event", Mock(return_value=None))
    monkeypatch.setattr(tasks, "notify", Mock())
    monkeypatch.setattr(tasks.threading, "Thread", Mock())
    monkeypatch.setattr(tasks.scheduler, "heartbeat", Mock(return_value=True))
    generate = Mock(side_effect=lambda **_: iter([{"event": "message_end"}]))
    monkeypatch.setattr(tasks, "AgentAppGenerator", Mock(return_value=Mock(generate=generate)))
    monkeypatch.setattr(tasks.execute, "apply_async", Mock())
    monkeypatch.setattr(tasks.dispatch, "delay", Mock())
    monkeypatch.setattr(tasks.advance_followups, "delay", Mock())
    return generate


@pytest.mark.parametrize("lease_present", [False, True])
@pytest.mark.parametrize("ending", ["failed", "cancelled", "interrupted"])
def test_successor_waits_for_durable_cleanup_even_without_a_redis_reservation(
    database, monkeypatch, lease_present, ending
):
    from tasks import workbench_tasks as tasks

    factory, tenant, account, chat_id, parent_id, _, fence = database
    fence.return_value = False
    child_id = str(uuid4())
    with factory.begin() as session:
        parent = session.get(WorkbenchRun, parent_id)
        parent.status, parent.created_at = ending, datetime(2026, 9, 16)
        ticket = parent.backend_run_id
        session.add(
            WorkbenchRun(
                id=child_id,
                tenant_id=tenant,
                account_id=account,
                chat_id=chat_id,
                revision_id=parent.revision_id,
                request_key="continue",
                status="queued",
                event_log="[]",
                created_at=parent.created_at + timedelta(seconds=1),
                payload=json.dumps({"query": "继续", "recovery": {"attempt": 0}, "branch_parent_run_id": parent_id}),
            )
        )
    generate = stub_executor(monkeypatch, tenant)
    monkeypatch.setattr(
        tasks.redis_client,
        "zscore",
        lambda _, identifier: 9999999999 if lease_present and identifier == parent_id else None,
    )
    with Flask(__name__).app_context():
        tasks.execute.run(f"{tenant}:{account}", child_id)
    generate.assert_not_called()
    tasks.execute.apply_async.assert_called_once()
    # Persist the remote acknowledgement, including a response without history.
    followups.save_fenced_state(ticket, {"history": None, "status": "cancelled"})
    monkeypatch.setattr(tasks.redis_client, "zscore", lambda *_: None)
    with Flask(__name__).app_context():
        tasks.execute.run(f"{tenant}:{account}", child_id)
    generate.assert_called_once()


def stub_reconcile(monkeypatch):
    from tasks import workbench_tasks as tasks

    monkeypatch.setattr(tasks.redis_client, "zrangebyscore", lambda *_: [])
    monkeypatch.setattr(tasks.redis_client, "zrange", lambda *_: [])
    monkeypatch.setattr(tasks.redis_client, "zscore", lambda *_: None)
    monkeypatch.setattr(tasks, "stop_native", Mock())
    monkeypatch.setattr(tasks.maintenance, "gated_owners", lambda: set())
    monkeypatch.setattr(tasks.dispatch, "delay", Mock())
    monkeypatch.setattr(tasks.recover_run, "delay", Mock())


def test_reconcile_rotates_unconfirmed_cancelled_tickets_after_redis_loss(database, monkeypatch, config_overrides):
    from tasks import workbench_tasks as tasks

    factory, tenant, account, chat_id, run_id, _, fence = database
    config_overrides(WORKBENCH_ENABLED=True)
    fence.return_value = False
    with factory.begin() as session:
        original = session.get(WorkbenchRun, run_id)
        original.status = "cancelled"
        for _ in range(200):
            session.add(
                WorkbenchRun(
                    id=str(uuid4()),
                    tenant_id=tenant,
                    account_id=account,
                    chat_id=chat_id,
                    revision_id=original.revision_id,
                    request_key=str(uuid4()),
                    status="cancelled",
                    backend_run_id=str(uuid4()),
                    payload="{}",
                    event_log="[]",
                )
            )
    stub_reconcile(monkeypatch)
    tasks.reconcile.run()
    assert fence.call_count == 200
    tasks.reconcile.run()
    assert len({call.args[0] for call in fence.call_args_list}) == 201


def test_cleanup_proof_matches_the_current_attempt_and_keeps_saved_history(database):
    factory, _, _, _, run_id, _, _ = database
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        run.status = "cancelled"
        ticket = run.backend_run_id
        payload = json.loads(run.payload)
        payload["steering_delivered_ids"] = ["consumed"]
        run.payload = json.dumps(payload)
    followups.save_fenced_state(ticket, {"history": None})
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        saved = json.loads(run.payload)
        assert saved["cleanup_confirmed_ticket"] == ticket
        assert saved["output_history"] == {"messages": []}
        assert saved["steering_delivered_ids"] == ["consumed"]
        assert session.scalar(select(WorkbenchRun.id).where(recovery.cleanup_pending_condition())) is None
        run.backend_run_id = str(uuid4())
    followups.save_fenced_state(ticket, {"history": {"messages": []}, "steering_delivered_ids": []})
    with factory() as session:
        assert session.scalar(select(WorkbenchRun.id).where(recovery.cleanup_pending_condition())) == run_id
        assert json.loads(session.get(WorkbenchRun, run_id).payload)["steering_delivered_ids"] == ["consumed"]


@pytest.mark.parametrize("reply", [{"run_id": "wrong", "status": "cancelled"}, {"run_id": "same", "status": "unknown"}])
def test_invalid_remote_cleanup_response_cannot_confirm_a_ticket(database, monkeypatch, config_overrides, reply):
    import httpx

    from tasks import workbench_tasks as tasks

    factory, _, _, _, run_id, _, fence = database
    with factory.begin() as session:
        run = session.get(WorkbenchRun, run_id)
        run.status = "cancelled"
        ticket = run.backend_run_id
    config_overrides(AGENT_BACKEND_BASE_URL="http://agent.test", AGENT_BACKEND_API_TOKEN="test-token")
    actual_client = httpx.Client
    body = {**reply, "run_id": ticket if reply["run_id"] == "same" else reply["run_id"]}
    monkeypatch.setattr(
        tasks.httpx,
        "Client",
        lambda **kwargs: actual_client(
            **kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
        ),
    )
    with pytest.raises(ValueError):
        fence.real_function(ticket)
    with factory() as session:
        assert session.scalar(select(WorkbenchRun.id).where(recovery.cleanup_pending_condition())) == run_id
