"""History timestamps must survive list/detail/rename DTO serialization."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.exceptions import NotFound

from controllers.console.workbench import WorkbenchChatListResponse, WorkbenchChatResponse, WorkbenchChatSummaryResponse
from core.db import session_factory as factory_module
from models.base import TypeBase
from models.workbench import WorkbenchChat, WorkbenchRevision, WorkbenchRun
from services.workbench import service


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[sessionmaker[Session], str, str, str]]:
    engine = create_engine("sqlite://")
    TypeBase.metadata.create_all(
        engine,
        tables=[
            TypeBase.metadata.tables[model.__tablename__] for model in (WorkbenchChat, WorkbenchRevision, WorkbenchRun)
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(factory_module, "_session_maker", factory)
    monkeypatch.setattr(service, "authorize", Mock())
    tenant, account, chat_id = [str(uuid4()) for _ in range(3)]
    with factory.begin() as session:
        for identifier, owner, deleted in (
            (chat_id, account, 0),
            (str(uuid4()), str(uuid4()), 0),
            (str(uuid4()), account, 1),
        ):
            session.add(
                WorkbenchChat(
                    id=identifier,
                    tenant_id=tenant,
                    account_id=owner,
                    agent_id=str(uuid4()),
                    app_id=str(uuid4()),
                    base_snapshot_id=str(uuid4()),
                    title="历史会话",
                    version=1,
                    deleted=deleted,
                    created_at=datetime(2026, 2, 1),
                    updated_at=datetime(2026, 8, 1),
                )
            )
        session.add(
            WorkbenchRevision(
                id=str(uuid4()),
                tenant_id=tenant,
                account_id=account,
                chat_id=chat_id,
                version=1,
                selection=json.dumps(
                    {
                        "model": "model",
                        "tools": [],
                        "skills": [],
                        "knowledge": [],
                        "model_parameters": {},
                        "tool_parameters": {},
                    }
                ),
                effective_soul="{}",
            )
        )
    try:
        yield factory, tenant, account, chat_id
    finally:
        engine.dispose()


def test_list_and_detail_return_utc_seconds_without_exposing_other_owners(
    history: tuple[sessionmaker[Session], str, str, str],
) -> None:
    _, tenant, account, chat_id = history
    chats = service.list_chats(tenant, account)
    assert [chat["id"] for chat in chats] == [chat_id]
    listed = WorkbenchChatListResponse.model_validate({"data": chats}).model_dump(mode="json")["data"][0]
    detail = WorkbenchChatResponse.model_validate(service.read_chat(tenant, account, chat_id)).model_dump(mode="json")
    for response in (listed, detail):
        assert response["created_at"] == int(datetime(2026, 2, 1, tzinfo=UTC).timestamp())
        assert response["updated_at"] == int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())
    with pytest.raises(NotFound):
        service.read_chat(tenant, str(uuid4()), chat_id)


def test_rename_returns_the_flushed_update_time(history: tuple[sessionmaker[Session], str, str, str]) -> None:
    factory, tenant, account, chat_id = history
    renamed = WorkbenchChatSummaryResponse.model_validate(
        service.update_chat(tenant, account, chat_id, title="修改后的名称")
    )
    with factory() as session:
        stored = session.get(WorkbenchChat, chat_id)
        assert stored is not None
        assert renamed.title == stored.title == "修改后的名称"
        assert renamed.updated_at == int(stored.updated_at.replace(tzinfo=UTC).timestamp())
        assert renamed.created_at == int(stored.created_at.replace(tzinfo=UTC).timestamp())
    assert renamed.updated_at > int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())


def test_timestamp_contract_documents_required_epoch_seconds() -> None:
    schema = WorkbenchChatSummaryResponse.model_json_schema(mode="serialization")
    for field in ("created_at", "updated_at"):
        assert field in schema["required"]
        assert schema["properties"][field]["type"] == "integer"
        assert "Unix seconds" in schema["properties"][field]["description"]


@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "running",
        "environment_installing",
        "stopping",
        "waiting_input",
        "environment_update",
        "completed",
        "failed",
        "cancelled",
    ],
)
def test_history_executing_state(history, status):
    factory, tenant, account, chat_id = history
    with factory.begin() as session:
        session.add(
            WorkbenchRun(
                id=str(uuid4()),
                tenant_id=tenant,
                account_id=account,
                chat_id=chat_id,
                revision_id=str(uuid4()),
                request_key=str(uuid4()),
                payload="{}",
                status=status,
            )
        )
    expected = status in {"queued", "running", "environment_installing", "stopping"}
    listed = WorkbenchChatSummaryResponse.model_validate(service.list_chats(tenant, account)[0])
    assert listed.is_running is expected
    assert listed.needs_input is (status == "waiting_input")
    expected_active = expected or status in {"environment_update", "waiting_input"}
    assert listed.has_active_run is expected_active
    assert service.read_chat(tenant, account, chat_id)["is_running"] is expected
    assert service.read_chat(tenant, account, chat_id)["needs_input"] is (status == "waiting_input")
    assert service.read_chat(tenant, account, chat_id)["has_active_run"] is expected_active
    assert service.update_chat(tenant, account, chat_id, pinned=True)["is_running"] is expected
    assert service.update_chat(tenant, account, chat_id, pinned=True)["has_active_run"] is expected_active


def test_history_executing_state_ignores_foreign_run_owners(history):
    factory, tenant, account, chat_id = history
    with factory.begin() as session:
        for run_tenant, run_account in ((str(uuid4()), account), (tenant, str(uuid4()))):
            session.add(
                WorkbenchRun(
                    id=str(uuid4()),
                    tenant_id=run_tenant,
                    account_id=run_account,
                    chat_id=chat_id,
                    revision_id=str(uuid4()),
                    request_key=str(uuid4()),
                    payload="{}",
                    status="running",
                )
            )
    assert service.list_chats(tenant, account)[0]["is_running"] is False
    assert service.read_chat(tenant, account, chat_id)["is_running"] is False
    assert service.list_chats(tenant, account)[0]["needs_input"] is False
    assert service.list_chats(tenant, account)[0]["has_active_run"] is False


def test_favorites_keep_their_chronological_position_and_activity_time(
    history: tuple[sessionmaker[Session], str, str, str],
) -> None:
    factory, tenant, account, chat_id = history
    newer_id = str(uuid4())
    with factory.begin() as session:
        original = session.get(WorkbenchChat, chat_id)
        assert original is not None
        session.add(
            WorkbenchChat(
                id=newer_id,
                tenant_id=tenant,
                account_id=account,
                agent_id=original.agent_id,
                app_id=original.app_id,
                base_snapshot_id=original.base_snapshot_id,
                title="最近的会话",
                version=1,
                created_at=datetime(2026, 9, 1),
                updated_at=datetime(2026, 9, 14),
            )
        )
    original_time = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())
    for pinned in (True, False):
        result = service.update_chat(tenant, account, chat_id, pinned=pinned)
        assert result["pinned"] is pinned
        assert result["updated_at"] == original_time
        chats = service.list_chats(tenant, account)
        assert [chat["id"] for chat in chats] == [newer_id, chat_id]
        assert chats[-1]["updated_at"] == original_time


def test_history_limit_selects_recent_chats_before_favorites(
    history: tuple[sessionmaker[Session], str, str, str],
) -> None:
    factory, tenant, account, chat_id = history
    with factory.begin() as session:
        original = session.get(WorkbenchChat, chat_id)
        assert original is not None
        original.pinned = True
        original.updated_at = datetime(2026, 8, 2)
        for index in range(200):
            session.add(
                WorkbenchChat(
                    id=str(uuid4()),
                    tenant_id=tenant,
                    account_id=account,
                    agent_id=original.agent_id,
                    app_id=original.app_id,
                    base_snapshot_id=original.base_snapshot_id,
                    title=f"最近的会话 {index}",
                    version=1,
                    created_at=datetime(2026, 9, 1),
                    updated_at=datetime(2026, 9, 14),
                )
            )
    chats = service.list_chats(tenant, account)
    assert len(chats) == 200
    assert all(chat["id"] != chat_id for chat in chats)


def test_new_message_refreshes_history_time_but_idempotent_retry_does_not(
    history: tuple[sessionmaker[Session], str, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.workbench import branches, mentions, scheduler

    factory, tenant, account, chat_id = history
    with factory() as session:
        chat = session.get(WorkbenchChat, chat_id)
        assert chat is not None
        base: service.WorkbenchTemplate = {
            "agent_id": chat.agent_id,
            "app_id": chat.app_id,
            "snapshot_id": chat.base_snapshot_id,
            "soul": {},
        }
    monkeypatch.setattr(service, "template", Mock(return_value=base))
    monkeypatch.setattr(service, "compile_config", Mock(return_value={}))
    monkeypatch.setattr(mentions, "default_capabilities", lambda _soul, selection: selection)
    monkeypatch.setattr(
        mentions,
        "resolve_mentions",
        Mock(return_value={"resource_mentions": {"tools": [], "skills": [], "knowledge": []}}),
    )
    monkeypatch.setattr(branches, "resolve_parent", Mock(return_value={}))
    publish = Mock()
    monkeypatch.setattr(scheduler, "publish", publish)
    sent_at = datetime(2026, 9, 14, 7)
    monkeypatch.setattr(service, "naive_utc_now", lambda: sent_at)

    sent = service.enqueue(tenant, account, chat_id, 1, "same-request", {"query": "继续整理"})
    assert service.list_chats(tenant, account)[0]["updated_at"] == int(sent_at.replace(tzinfo=UTC).timestamp())
    monkeypatch.setattr(service, "naive_utc_now", lambda: datetime(2026, 9, 15, 7))
    retried = service.enqueue(tenant, account, chat_id, 1, "same-request", {"query": "继续整理"})
    assert retried["id"] == sent["id"]
    assert service.list_chats(tenant, account)[0]["updated_at"] == int(sent_at.replace(tzinfo=UTC).timestamp())
    publish.assert_called_once()
