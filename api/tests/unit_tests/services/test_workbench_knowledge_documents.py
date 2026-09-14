"""Exercise document pagination and authorization against real SQL queries."""

import json
from collections.abc import Callable
from uuid import uuid4

import pytest
from flask import Flask
from sqlalchemy.orm import Session
from werkzeug.exceptions import BadRequest, Conflict, Forbidden, NotFound

from models.dataset import Dataset, Document, DocumentSegment
from models.enums import SegmentStatus
from models.workbench import WorkbenchChat, WorkbenchRun
from services.entities.knowledge_documents import KnowledgeDocumentsPayload
from services.entities.knowledge_retrieval_inner import InnerKnowledgeRetrieveCaller
from services.workbench import knowledge_documents as service

Context = tuple[Session, KnowledgeDocumentsPayload, WorkbenchChat, WorkbenchRun, Dataset, list[dict[str, str]]]


@pytest.fixture
def context(sqlite_session: Session, monkeypatch: pytest.MonkeyPatch, config_overrides: Callable[..., None]) -> Context:
    config_overrides(WORKBENCH_ENABLED=True)
    tenant, account, app, dataset_id = [str(uuid4()) for _ in range(4)]
    chat = WorkbenchChat(
        tenant_id=tenant,
        account_id=account,
        app_id=app,
        agent_id=str(uuid4()),
        base_snapshot_id=str(uuid4()),
    )
    sqlite_session.add(chat)
    sqlite_session.flush()
    run = WorkbenchRun(
        tenant_id=tenant,
        account_id=account,
        chat_id=chat.id,
        revision_id=str(uuid4()),
        request_key="read-documents",
        status="running",
        payload=json.dumps({"effective_soul": {"knowledge": {"sets": [{"datasets": [{"id": dataset_id}]}]}}}),
    )
    dataset = Dataset(id=dataset_id, tenant_id=tenant, name="资料库", created_by=account, provider="vendor")
    sqlite_session.add_all([run, dataset])
    sqlite_session.commit()
    visible = [{"id": dataset_id}]
    monkeypatch.setattr(service, "available_sets", lambda _tenant, _account: visible)
    request = KnowledgeDocumentsPayload(
        workbench_run_id=run.id,
        dataset_id=dataset_id,
        operation="list",
        caller=InnerKnowledgeRetrieveCaller(
            tenant_id=tenant, user_id=account, app_id=app, user_from="account", invoke_from="web-app"
        ),
    )
    return sqlite_session, request, chat, run, dataset, visible


def add_document(context: Context, position: int = 1, **overrides: object) -> Document:
    session, request, *_ = context
    values: dict[str, object] = {
        "tenant_id": request.caller.tenant_id,
        "dataset_id": request.dataset_id,
        "position": position,
        "name": f"资料{position}",
        "batch": "batch",
        "created_from": "web",
        "data_source_type": "upload_file",
        "created_by": request.caller.user_id,
        "enabled": True,
        "archived": False,
        "indexing_status": "completed",
    }
    values.update(overrides)
    doc = Document(**values)
    session.add(doc)
    session.flush()
    return doc


def add_segment(context: Context, doc: Document, position: int, content: str, **overrides: object) -> DocumentSegment:
    session, request, *_ = context
    segment = DocumentSegment(
        tenant_id=request.caller.tenant_id,
        dataset_id=request.dataset_id,
        document_id=doc.id,
        position=position,
        content=content,
        word_count=len(content),
        tokens=1,
        created_by=request.caller.user_id,
        status=SegmentStatus.COMPLETED,
    )
    for name, value in overrides.items():
        setattr(segment, name, value)
    session.add(segment)
    session.flush()
    return segment


def test_document_list_pages_without_omissions_and_reports_unindexed(context: Context) -> None:
    session, request, *_ = context
    expected = [add_document(context, i) for i in range(23)]
    expected[-1].indexing_status = "waiting"
    add_document(context, 24, enabled=False)
    add_document(context, 25, archived=True)
    add_document(context, 26, dataset_id=str(uuid4()))
    session.commit()
    first = service.read_documents(session, request)
    assert len(first.documents) == 20
    assert not first.complete
    assert first.total == 23
    assert first.unavailable_count == 1
    second = service.read_documents(session, request.model_copy(update={"cursor": first.next_cursor}))
    assert second.complete
    assert second.next_cursor is None
    assert [item.id for item in first.documents + second.documents] == [doc.id for doc in expected]
    assert not second.documents[-1].readable


def test_read_reassembles_long_segments_answers_and_more_than_twenty_segments(context: Context) -> None:
    session, request, *_ = context
    doc = add_document(context)
    long_text = "中文正文" * 6500 + "结尾证据"
    expected = [add_segment(context, doc, 0, long_text, answer="问答结尾")]
    expected.extend(add_segment(context, doc, i, f"第{i}段") for i in range(1, 25))
    add_segment(context, doc, 26, "disabled", enabled=False)
    add_segment(context, doc, 27, "waiting", status="waiting")
    session.commit()
    request = request.model_copy(update={"operation": "read", "document_id": doc.id})
    collected: dict[str, str] = {}
    page_count = 0
    while True:
        page = service.read_documents(session, request)
        page_count += 1
        assert page_count < 10
        assert page.total == 25
        assert page.unavailable_count == 2
        assert sum(len(item.content) for item in page.segments) <= 12000
        assert len(page.segments) <= 20
        for item in page.segments:
            assert item.offset == len(collected.get(item.id, ""))
            collected[item.id] = collected.get(item.id, "") + item.content
        if page.complete:
            break
        request = request.model_copy(update={"cursor": page.next_cursor})
    assert page_count >= 4
    assert list(collected) == [segment.id for segment in expected]
    assert list(collected.values()) == [service._segment_text(segment) for segment in expected]


@pytest.mark.parametrize("change", ["tenant", "account", "app", "run", "finished", "deleted", "unselected", "revoked"])
def test_rejects_inaccessible_runs_and_rechecks_permissions(context: Context, change: str) -> None:
    session, request, chat, run, dataset, visible = context
    add_document(context)
    if change in {"tenant", "account", "app"}:
        field = {"tenant": "tenant_id", "account": "user_id", "app": "app_id"}[change]
        request = request.model_copy(update={"caller": request.caller.model_copy(update={field: str(uuid4())})})
    elif change == "run":
        request = request.model_copy(update={"workbench_run_id": str(uuid4())})
    elif change == "finished":
        run.status = "succeeded"
    elif change == "deleted":
        chat.deleted = 1
    elif change == "unselected":
        run.payload = "{}"
    else:
        visible.clear()
    session.commit()
    with pytest.raises(Forbidden):
        service.read_documents(session, request)


def test_document_must_belong_to_dataset_and_be_indexed(context: Context) -> None:
    session, request, *_ = context
    other = add_document(context, dataset_id=str(uuid4()))
    request = request.model_copy(update={"operation": "read", "document_id": other.id})
    with pytest.raises(NotFound):
        service.read_documents(session, request)
    pending = add_document(context, indexing_status="waiting")
    with pytest.raises(Conflict):
        service.read_documents(session, request.model_copy(update={"document_id": pending.id}))


def test_cursor_is_scoped_and_detects_changed_content(context: Context) -> None:
    session, request, *_ = context
    doc = add_document(context)
    segment = add_segment(context, doc, 1, "a" * 13000)
    request = request.model_copy(update={"operation": "read", "document_id": doc.id})
    page = service.read_documents(session, request)
    with pytest.raises(BadRequest):
        service.read_documents(session, request.model_copy(update={"cursor": "not-base64"}))
    other = add_document(context, 2)
    with pytest.raises(BadRequest):
        service.read_documents(
            session, request.model_copy(update={"document_id": other.id, "cursor": page.next_cursor})
        )
    segment.content += "changed"
    session.flush()
    with pytest.raises(Conflict):
        service.read_documents(session, request.model_copy(update={"cursor": page.next_cursor}))


def test_saved_metadata_filter_applies_to_list_and_read(context: Context, monkeypatch: pytest.MonkeyPatch) -> None:
    session, request, _, _, dataset, _ = context
    dataset.retrieval_model = {
        "metadata_filtering_conditions": {
            "logical_operator": "and",
            "conditions": [{"name": "category", "comparison_operator": "is", "value": "public"}],
        }
    }
    allowed = add_document(context, 1)
    excluded = add_document(context, 2)
    calls: list[dict[str, object]] = []

    def filter_documents(_self, **kwargs: object) -> tuple[dict[str, list[str]], None]:
        calls.append(kwargs)
        return {dataset.id: [allowed.id]}, None

    monkeypatch.setattr(service.DatasetRetrieval, "get_metadata_filter_condition", filter_documents)
    page = service.read_documents(session, request)
    assert [item.id for item in page.documents] == [allowed.id]
    with pytest.raises(NotFound):
        service.read_documents(session, request.model_copy(update={"operation": "read", "document_id": excluded.id}))
    assert len(calls) == 2
    assert calls[0]["metadata_filtering_mode"] == "manual"
    conditions = calls[0]["metadata_filtering_conditions"]
    assert isinstance(conditions, service.MetadataFilteringCondition)
    assert conditions.conditions is not None
    assert conditions.conditions[0].value == "public"


def test_external_dataset_does_not_claim_full_coverage(context: Context) -> None:
    session, request, _, _, dataset, _ = context
    dataset.provider = "external"
    session.flush()
    with pytest.raises(Conflict, match="外部知识库"):
        service.read_documents(session, request)


@pytest.mark.parametrize("case", ["valid", "missing_key", "invalid_payload"])
def test_inner_api_document_route_auth_validation_and_real_serialization(
    context: Context, config_overrides: Callable[..., None], case: str
) -> None:
    from controllers.inner_api import bp

    session, request, *_ = context
    doc = add_document(context)
    add_segment(context, doc, 1, "可核验的中文正文")
    session.commit()
    config_overrides(PLUGIN_DAEMON_KEY="daemon-key", INNER_API_KEY_FOR_PLUGIN="inner-key")
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(bp)
    payload = request.model_copy(update={"operation": "read", "document_id": doc.id}).model_dump(mode="json")
    headers: dict[str, str] = {"X-Inner-Api-Key": "inner-key"} if case != "missing_key" else {}
    if case == "invalid_payload":
        payload.pop("document_id")
    response = app.test_client().post("/inner/api/knowledge/documents", json=payload, headers=headers)
    assert response.status_code == {"valid": 200, "missing_key": 404, "invalid_payload": 400}[case]
    if case == "valid":
        body = response.get_json()
        assert body["segments"][0]["content"] == "可核验的中文正文"
        assert body["complete"] is True
        assert body["dataset_id"] == request.dataset_id
