import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import ANY, Mock

import pytest
from dify_agent.protocol.schemas import ContextStatusData, ContextStatusRunEvent

from services.workbench import context_status
from tests.unit_tests.config_override import apply_config_overrides


def setup(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> tuple[SimpleNamespace, Mock]:
    run = SimpleNamespace(
        id="workbench",
        tenant_id="tenant",
        account_id="account",
        backend_run_id="native",
        payload=json.dumps({"effective_soul": {"model": {"model_provider": "provider", "model": "model"}}}),
        event_log="[]",
    )
    for key, value in overrides.items():
        setattr(run, key, value)
    factory = Mock()
    factory.begin.return_value = nullcontext(Mock())
    monkeypatch.setattr(context_status.session_factory, "get_session_maker", lambda: factory)
    monkeypatch.setattr(context_status, "current_run", Mock(return_value=run))
    apply_config_overrides(monkeypatch, WORKBENCH_ENABLED=True)
    redis = Mock()
    redis.xadd.return_value = b"12-0"
    monkeypatch.setattr(context_status, "redis_client", redis)
    return run, redis


def event(phase: Literal["usage", "compacting", "compacted", "failed"] = "usage") -> ContextStatusRunEvent:
    return ContextStatusRunEvent(
        run_id="native",
        data=ContextStatusData(
            phase=phase,
            used_tokens=500,
            window_tokens=1000,
            before_tokens=850,
            compaction_id="cycle" if phase != "usage" else None,
        ),
    )


def test_persists_latest_reading_and_merges_compaction_in_stream_order(monkeypatch: pytest.MonkeyPatch) -> None:
    run, redis = setup(monkeypatch)
    context_status.record_context_status("tenant", "conversation", "account", event("compacted"))
    cast(Mock, context_status.current_run).assert_called_once_with(
        ANY,
        "tenant",
        "conversation",
        "account",
        for_update=True,
    )
    payload = json.loads(run.payload)
    assert payload["context_usage"]["model"] == "provider::model"
    run.event_log = json.dumps([{"event": "agent_message", "_id": "11-0"}, {"event": "agent_message", "_id": "13-0"}])
    assert [item["_id"] for item in context_status.merge_context_events(run, payload)] == ["11-0", "12-0", "13-0"]
    assert redis.xadd.call_args.args[0].endswith("workbench")
    context_status.record_context_status("tenant", "conversation", "account", event())
    assert len(json.loads(run.payload)["context_events"]) == 1


def test_ignores_wrong_backend_attempt_and_other_account(monkeypatch: pytest.MonkeyPatch) -> None:
    for overrides in [{"backend_run_id": "old-attempt"}, {"account_id": "other"}, {"tenant_id": "other"}]:
        run, redis = setup(monkeypatch, **overrides)
        original = run.payload
        context_status.record_context_status("tenant", "conversation", "account", event("compacted"))
        redis.xadd.assert_not_called()
        assert run.payload == original
