import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.workbench import titles


def setup(
    monkeypatch: pytest.MonkeyPatch, title: str = "新会话", native_name: str = "合同审查要点"
) -> tuple[SimpleNamespace, Mock, Mock]:
    chat = SimpleNamespace(id="chat", title=title, app_id="app", account_id="account")
    session = Mock()
    session.scalar.side_effect = [chat, SimpleNamespace(name=native_name), "run"]
    factory = Mock()
    factory.begin.return_value = nullcontext(session)
    monkeypatch.setattr(titles.session_factory, "get_session_maker", lambda: factory)
    redis = Mock()
    monkeypatch.setattr(titles, "redis_client", redis)
    return chat, session, redis


def test_syncs_native_ai_title_and_sends_owned_chat_event(monkeypatch: pytest.MonkeyPatch) -> None:
    chat, session, redis = setup(monkeypatch)
    titles.sync_native_title("tenant", "native")
    assert chat.title == "合同审查要点"
    assert session.scalar.call_args_list[0].args[0].compile().params == {
        "tenant_id_1": "tenant",
        "conversation_id_1": "native",
        "deleted_1": 0,
    }
    assert session.scalar.call_args_list[1].args[0].compile().params == {
        "id_1": "native",
        "app_id_1": "app",
        "from_account_id_1": "account",
    }
    assert json.loads(redis.xadd.call_args.args[1]["data"]) == {
        "event": "workbench_chat_title",
        "chat_id": "chat",
        "title": "合同审查要点",
    }


def test_late_name_does_not_overwrite_manual_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    chat, session, redis = setup(monkeypatch, title="我的案件材料")
    titles.sync_native_title("tenant", "native")
    assert chat.title == "我的案件材料"
    assert session.scalar.call_count == 1
    redis.xadd.assert_not_called()


def test_default_native_title_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    chat, session, redis = setup(monkeypatch, native_name="New conversation")
    titles.sync_native_title("tenant", "native")
    assert chat.title == "新会话"
    redis.xadd.assert_not_called()
