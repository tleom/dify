from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from werkzeug.exceptions import BadRequest, NotFound

from services.workbench import directories


def test_folder_uses_existing_binding_and_new_chat_has_stable_folder():
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


def test_directory_listing_filters_owner_and_deletion():
    session = Mock()
    session.scalars.return_value = [SimpleNamespace(id="chat", conversation_id=None)]
    assert list(directories.owned_directories(session, "tenant", "account")) == ["conversations/chat"]
    assert session.scalars.call_args.args[0].compile().params == {
        "tenant_id_1": "tenant",
        "account_id_1": "account",
        "deleted_1": 0,
    }


def test_attachment_cannot_reference_another_owned_conversation(monkeypatch):
    chat = SimpleNamespace(id="chat-a")
    monkeypatch.setattr(directories, "owned_directories", lambda *_args: {"conversations/a": chat})
    assert directories.resolve_path(None, "tenant", "account", "conversations/a/file.txt", chat_id="chat-a") == (
        "conversations/a",
        chat,
    )
    with pytest.raises(NotFound):
        directories.resolve_path(None, "tenant", "account", "conversations/a/file.txt", chat_id="chat-b")
    with pytest.raises(NotFound):
        directories.resolve_path(None, "tenant", "account", "conversations/foreign/file.txt")


@pytest.mark.parametrize(
    "path",
    [
        "shared/file.txt",
        "conversations/a/../b/file",
        "conversations/a//file",
        "conversations/a/./file",
        "conversations/a/\\file",
    ],
)
def test_rejects_legacy_or_noncanonical_paths(path, monkeypatch):
    monkeypatch.setattr(directories, "owned_directories", lambda *_args: {})
    with pytest.raises((BadRequest, NotFound)):
        directories.resolve_path(None, "tenant", "account", path)


def test_title_only_affects_download_filename():
    assert directories.archive_name("合同/审查:结果") == "合同审查结果.zip"
