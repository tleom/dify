"""Cancelled installs must release queue gates only after the installer is terminal."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.workbench import maintenance

TENANT = "e3d9600a-74e4-4a8d-876f-26426e207886"
ACCOUNT = "83b9990d-0f28-428f-bc1f-1d0ce1811d19"


@pytest.mark.parametrize(
    ("status", "finished"), [("installing", False), ("ready", True), ("failed", True), ("unknown", False)]
)
def test_cancelled_installation_checks_stable_request_and_owner(status, finished):
    session = Mock()
    session.scalars.return_value = [
        SimpleNamespace(
            id="cancelled-run",
            payload=json.dumps({"attempt": 2, "pending": {"tool_name": "update_shared_environment"}}),
        )
    ]
    manager = Mock(return_value={"status": status})
    assert maintenance.cancelled_installations_finished(session, TENANT, ACCOUNT, manager) is finished
    assert manager.call_args.args[1:] == ("environment-status", {"request_id": "cancelled-run:2"})
    query = session.scalars.call_args.args[0].compile()
    assert TENANT in query.params.values()
    assert ACCOUNT in query.params.values()
    assert ["cancelled", "failed", "interrupted"] in query.params.values()


def test_manager_disconnect_keeps_gate_closed():
    session = Mock()
    session.scalars.return_value = [
        SimpleNamespace(id="run", payload=json.dumps({"pending": {"tool_name": "update_shared_environment"}}))
    ]
    with pytest.raises(ConnectionError):
        maintenance.cancelled_installations_finished(session, TENANT, ACCOUNT, Mock(side_effect=ConnectionError))


def test_old_non_pending_mentions_do_not_poll_installers():
    session = Mock()
    session.scalars.return_value = [
        SimpleNamespace(id="run", payload=json.dumps({"query": "update_shared_environment"}))
    ]
    manager = Mock()
    assert maintenance.cancelled_installations_finished(session, TENANT, ACCOUNT, manager)
    manager.assert_not_called()


def test_recovery_finds_gates_even_when_no_environment_runs_remain(monkeypatch):
    prefix = maintenance.scheduler.PREFIX + "maintenance:"
    redis = Mock()
    redis.scan_iter.return_value = [(prefix + TENANT + ":" + ACCOUNT).encode(), prefix + "invalid"]
    monkeypatch.setattr(maintenance, "redis_client", redis)
    assert maintenance.gated_owners() == {(TENANT, ACCOUNT)}
