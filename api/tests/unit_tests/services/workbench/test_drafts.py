"""Draft defaults and voice composition must not persist empty conversations."""
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from werkzeug.exceptions import Forbidden, NotFound

from services.workbench import audio, service
from services.workbench.policy import Selection


@pytest.mark.parametrize(
    "recent", [None, Selection(model="recent-model", knowledge=["old"], model_parameters={"temperature": 0.3})]
)
def test_draft_defaults_read_personal_preferences_without_writes(monkeypatch, recent):
    session = Mock()
    session.scalar.return_value = SimpleNamespace(selection=recent.model_dump_json()) if recent else None
    monkeypatch.setattr(service.session_factory, "create_session", lambda: nullcontext(session))
    writer = Mock(side_effect=AssertionError("Draft must not open a write transaction"))
    monkeypatch.setattr(service.session_factory, "get_session_maker", writer)
    soul = {"model": {"model_provider": "provider", "model": "default"}, "config_skills": [{"name": "docx"}]}
    selected = service.default_selection("tenant", "account", {"soul": soul})
    assert selected.model == ("recent-model" if recent else "provider::default")
    assert selected.knowledge == []
    assert selected.skills == ["docx"]
    assert selected.model_parameters == ({"temperature": 0.3} if recent else {})
    session.add.assert_not_called()
    session.commit.assert_not_called()
    writer.assert_not_called()
    query = str(session.scalar.call_args.args[0])
    assert "tenant_id" in query
    assert "account_id" in query


def test_draft_voice_checks_template_owner_without_creating_a_chat(monkeypatch):
    state = {"open": False}
    app = SimpleNamespace(id="app", tenant_id="tenant")

    @contextmanager
    def session():
        state["open"] = True
        try:
            yield SimpleNamespace(get=lambda _model, _key: app)
        finally:
            state["open"] = False

    monkeypatch.setattr(audio, "authorize", Mock())
    template = Mock(return_value={"app_id": "app"})
    monkeypatch.setattr(audio, "template", template)
    monkeypatch.setattr(audio.session_factory, "create_session", session)
    owned = Mock(side_effect=AssertionError("Draft has no stored chat"))
    monkeypatch.setattr(audio, "_chat", owned)

    def transcribe(**kwargs):
        assert not state["open"]
        assert kwargs["end_user"] == "account"
        return {"text": "语音草稿"}

    invoke = Mock(side_effect=transcribe)
    monkeypatch.setattr(audio.AudioService, "_invoke_speech_to_text", invoke)
    assert audio.transcribe("tenant", "account", None, object()) == {"text": "语音草稿"}
    template.assert_called_once_with("tenant", "account")
    owned.assert_not_called()
    invoke.reset_mock()
    template.side_effect = Forbidden()
    with pytest.raises(Forbidden):
        audio.transcribe("tenant", "foreign", None, object())
    invoke.assert_not_called()
    owned.side_effect = NotFound()
    with pytest.raises(NotFound):
        audio.transcribe("tenant", "foreign", "chat", object())
    invoke.assert_not_called()
