"""Cancelled installs must release queue gates only after the installer is terminal."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.workbench import maintenance

TENANT = "e3d9600a-74e4-4a8d-876f-26426e207886"
ACCOUNT = "83b9990d-0f28-428f-bc1f-1d0ce1811d19"


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> Mock:
    session = Mock()
    session.is_open = False

    @contextmanager
    def open_session() -> Generator[Mock]:
        session.is_open = True
        try:
            yield session
        finally:
            session.is_open = False

    monkeypatch.setattr(maintenance.session_factory, "create_session", open_session)
    return session


@pytest.mark.parametrize(
    ("status", "finished"), [("installing", False), ("ready", True), ("failed", True), ("unknown", False)]
)
def test_cancelled_installation_checks_stable_request_and_owner(status: str, finished: bool, session: Mock) -> None:
    session.scalars.return_value = [
        SimpleNamespace(
            id="cancelled-run",
            payload=json.dumps({"attempt": 2, "pending": {"tool_name": "update_shared_environment"}}),
        )
    ]

    def poll(*_args: object) -> dict[str, str]:
        assert not session.is_open
        return {"status": status}

    manager = Mock(side_effect=poll)
    assert maintenance.cancelled_installations_finished(TENANT, ACCOUNT, manager) is finished
    assert manager.call_args.args[1:] == ("environment-status", {"request_id": "cancelled-run:2"})
    query = session.scalars.call_args.args[0].compile()
    assert TENANT in query.params.values()
    assert ACCOUNT in query.params.values()
    assert ["cancelled", "failed", "interrupted"] in query.params.values()


def test_manager_disconnect_keeps_gate_closed(session: Mock) -> None:
    session.scalars.return_value = [
        SimpleNamespace(id="run", payload=json.dumps({"pending": {"tool_name": "update_shared_environment"}}))
    ]
    with pytest.raises(ConnectionError):
        maintenance.cancelled_installations_finished(TENANT, ACCOUNT, Mock(side_effect=ConnectionError))
    assert not session.is_open


def test_old_non_pending_mentions_do_not_poll_installers(session: Mock) -> None:
    session.scalars.return_value = [
        SimpleNamespace(id="run", payload=json.dumps({"query": "update_shared_environment"}))
    ]
    manager = Mock()
    assert maintenance.cancelled_installations_finished(TENANT, ACCOUNT, manager)
    manager.assert_not_called()


def test_recovery_finds_gates_even_when_no_environment_runs_remain(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = maintenance.scheduler.PREFIX + "maintenance:"
    redis = Mock()
    redis.scan_iter.return_value = [(prefix + TENANT + ":" + ACCOUNT).encode(), prefix + "invalid"]
    monkeypatch.setattr(maintenance, "redis_client", redis)
    assert maintenance.gated_owners() == {(TENANT, ACCOUNT)}
