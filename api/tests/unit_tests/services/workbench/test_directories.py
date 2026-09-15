from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from werkzeug.exceptions import BadRequest, NotFound

from services.workbench import directories


def test_folder_uses_existing_binding_and_new_chat_has_stable_folder() -> None:
    session = Mock()
    chat = SimpleNamespace(id="chat-id", conversation_id=None, app_id="app")
    assert directories.chat_directory(session, chat) == "conversations/chat-id"
    session.scalar.assert_not_called()
    chat.conversation_id = "native-id"
    session.scalar.return_value = "existing-binding"
    assert directories.chat_directory(session, chat) == "conversations/existing-binding"
    assert session.scalar.call_args.args[0].compile().params == {
        "id_1": "native-id",
        "app_id_1": "app",
    }


def test_directory_listing_filters_owner_and_deletion() -> None:
    session = Mock()
    session.scalars.return_value = [SimpleNamespace(id="chat", conversation_id=None)]
    assert list(directories.owned_directories(session, "tenant", "account")) == ["conversations/chat"]
    assert session.scalars.call_args.args[0].compile().params == {
        "tenant_id_1": "tenant",
        "account_id_1": "account",
        "deleted_1": 0,
    }


def test_explicit_cross_conversation_paths_require_owned_source_and_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.workbench import service

    chat = SimpleNamespace(id="chat-a")

    def owned_chat(_session, _tenant, _account, identifier):
        if identifier not in {"chat-a", "chat-b"}:
            raise NotFound()
        return SimpleNamespace(id=identifier)

    monkeypatch.setattr(service, "_chat", owned_chat)
    monkeypatch.setattr(directories, "owned_directories", lambda *_args: {"conversations/a": chat})
    assert directories.resolve_path(None, "tenant", "account", "conversations/a/file.txt", chat_id="chat-a") == (
        "conversations/a",
        chat,
    )
    assert directories.resolve_path(None, "tenant", "account", "conversations/a/file.txt", chat_id="chat-b") == (
        "conversations/a",
        chat,
    )
    with pytest.raises(NotFound):
        directories.resolve_path(None, "tenant", "account", "memory.md", chat_id="foreign")
    with pytest.raises(NotFound):
        directories.resolve_path(None, "tenant", "account", "conversations/foreign/file.txt")


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/foreign",
        "conversations/a/../b/file",
        "conversations/a//file",
        "conversations/a/./file",
        "conversations/a/\\file",
    ],
)
def test_rejects_legacy_or_noncanonical_paths(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(directories, "owned_directories", lambda *_args: {})
    with pytest.raises((BadRequest, NotFound)):
        directories.resolve_path(None, "tenant", "account", path)


def test_title_only_affects_download_filename() -> None:
    assert directories.archive_name("合同/审查:结果") == "合同审查结果.zip"


def test_personal_root_memory_and_skills_are_valid_paths():
    for path in (".", "memory.md", "skills/report/SKILL.md", "shared/file.txt"):
        assert directories.resolve_path(None, "tenant", "account", path) == (".", None)
