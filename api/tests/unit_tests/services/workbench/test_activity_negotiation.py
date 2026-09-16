"""Old clients remain on their existing transcript protocol when producers are enabled."""

import json
from collections.abc import Callable
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from controllers.console.workbench import WorkbenchRegeneratePayload, WorkbenchRunPayload
from models.workbench import WorkbenchRun
from services.workbench import branches, mentions, personal_mcp, scheduler, service


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("requested", [None, 0, 1])
def test_enqueue_freezes_only_a_negotiated_and_enabled_protocol(
    monkeypatch: pytest.MonkeyPatch, config_overrides: Callable[..., None], enabled: bool, requested: int | None
) -> None:
    config_overrides(WORKBENCH_ACTIVITY_ENABLED=enabled)
    monkeypatch.setattr(service, "authorize", lambda *_args: None)
    monkeypatch.setattr(
        service, "template", lambda *_args: {"soul": {}, "agent_id": "agent", "snapshot_id": "snapshot"}
    )
    monkeypatch.setattr(service, "read_chat", lambda *_args: {"version": 1, "selection": {"model": "model"}})
    monkeypatch.setattr(service, "compile_config", lambda *_args: {})
    monkeypatch.setattr(
        service,
        "_chat",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="chat", tenant_id="tenant", account_id="account", version=1, agent_id="agent"
        ),
    )
    monkeypatch.setattr(mentions, "default_capabilities", lambda _soul, selection: selection)
    monkeypatch.setattr(personal_mcp, "catalog", Mock(return_value=[]))
    monkeypatch.setattr(branches, "resolve_parent", lambda *_args: {})
    session = Mock()
    empty_runs: list[object] = []
    session.scalars.return_value = empty_runs
    session.scalar.side_effect = [None, None, None, SimpleNamespace(id="revision"), None, None]
    monkeypatch.setattr(service.session_factory, "create_session", lambda: nullcontext(session))
    monkeypatch.setattr(
        service.session_factory, "get_session_maker", lambda: SimpleNamespace(begin=lambda: nullcontext(session))
    )
    monkeypatch.setattr(
        service,
        "run_dto",
        lambda run: {
            "id": run.id,
            "status": run.status,
            "activity_protocol": json.loads(run.payload)["activity_protocol"],
        },
    )
    monkeypatch.setattr(scheduler, "publish", Mock())
    payload: dict[str, str | int] = {"query": "hello"}
    if requested is not None:
        payload["activity_protocol"] = requested
    result = service.enqueue("tenant", "account", "chat", 1, "request", payload)
    assert result["activity_protocol"] == int(enabled and requested == 1)
    stored = next(call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], WorkbenchRun))
    assert json.loads(stored.payload)["activity_protocol"] == result["activity_protocol"]


@pytest.mark.parametrize("model", [WorkbenchRunPayload, WorkbenchRegeneratePayload])
def test_transport_defaults_to_legacy_and_rejects_unknown_protocol(
    model: type[WorkbenchRunPayload] | type[WorkbenchRegeneratePayload],
) -> None:
    assert model.model_validate({"version": 1, "request_key": "key", "query": "hello"}).activity_protocol == 0
    with pytest.raises(ValidationError):
        model.model_validate({"version": 1, "request_key": "key", "query": "hello", "activity_protocol": 2})
