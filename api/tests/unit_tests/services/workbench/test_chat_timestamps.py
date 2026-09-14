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
