import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from werkzeug.exceptions import Forbidden

from services.workbench import knowledge_events as events


def fixture(monkeypatch, *, query="问题", status="running"):
    run = SimpleNamespace(id="run", status=status, payload=json.dumps({"effective_soul": {"knowledge": {"sets": [{
        "id": "kb", "name": "知识库", "datasets": [{"id": "kb"}], "query": {"value": "问题"},
    }]}}}))
    session = MagicMock()
    session.__enter__.return_value = session
    session.scalar.return_value = run
    monkeypatch.setattr(events.session_factory, "get_session_maker", lambda: SimpleNamespace(begin=lambda: session))
    monkeypatch.setattr(events.dify_config, "WORKBENCH_ENABLED", True)
    redis = MagicMock()
    monkeypatch.setattr(events, "redis_client", redis)
    request = SimpleNamespace(workbench_run_id="run", query=query, dataset_ids=["kb"],
                              caller=SimpleNamespace(tenant_id="tenant", user_id="account", app_id="app"))
    return run, request, redis


def test_retrieval_record_updates_one_durable_entry_and_streams_results(monkeypatch):
    run, request, redis = fixture(monkeypatch)
    events.retrieval_event(request, "running")
    hits = [{"content": "返回片段", "metadata": {"document_name": "来源.docx"}}]
    events.retrieval_event(request, "returned", results=hits)
    saved = json.loads(run.payload)["knowledge_events"]
    assert len(saved) == 1
    assert saved[0]["results"] == hits
    assert saved[0]["status"] == "returned"
    assert redis.xadd.call_count == 2
    assert events.run_knowledge_events(run, json.loads(run.payload)) == saved


@pytest.mark.parametrize("query,status", [("其他问题", "running"), ("问题", "cancelled")])
def test_unrelated_or_stopped_retrieval_is_rejected(monkeypatch, query, status):
    run, request, redis = fixture(monkeypatch, query=query, status=status)
    with pytest.raises(Forbidden):
        events.retrieval_event(request, "running")
    redis.xadd.assert_not_called()


def test_old_run_without_knowledge_does_not_query_snapshot():
    assert events.run_knowledge_events(SimpleNamespace(), {}) == []
