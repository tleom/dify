from contextlib import contextmanager
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import BadGateway, BadRequest, Conflict, NotFound, RequestEntityTooLarge

from services.workbench import audio
from services.errors.audio import (
    AudioTooLargeServiceError,
    NoAudioUploadedServiceError,
    ProviderNotSupportSpeechToTextServiceError,
    UnsupportedAudioTypeServiceError,
)


@pytest.fixture
def voice(monkeypatch):
    state={'session_open':False}
    app=SimpleNamespace(tenant_id='tenant',id='app')
    @contextmanager
    def session():
        state['session_open']=True
        try:yield SimpleNamespace(get=lambda model,id:app)
        finally:state['session_open']=False
    monkeypatch.setattr(audio,'authorize',Mock())
    monkeypatch.setattr(audio.session_factory,'create_session',session)
    owned=Mock(return_value=SimpleNamespace(app_id='app'))
    monkeypatch.setattr(audio,'_chat',owned)
    invoke=Mock()
    monkeypatch.setattr(audio.AudioService,'_invoke_speech_to_text',invoke)
    return state,owned,invoke


def test_transcription_releases_database_connection_and_keeps_owner(voice):
    state,owned,invoke=voice
    def transcribe(**kwargs):
        assert not state['session_open']
        assert kwargs['end_user']=='account'
        return {'text':'语音识别结果'}
    invoke.side_effect=transcribe
    assert audio.transcribe('tenant','account','chat',object())=={'text':'语音识别结果'}
    assert owned.call_args.args[1:]==('tenant','account','chat')


def test_foreign_chat_cannot_send_audio_to_model(voice):
    _,owned,invoke=voice
    owned.side_effect=NotFound()
    with pytest.raises(NotFound):audio.transcribe('tenant','account','foreign',object())
    invoke.assert_not_called()


@pytest.mark.parametrize('cause,expected',[
    (NoAudioUploadedServiceError(),BadRequest),
    (UnsupportedAudioTypeServiceError(),BadRequest),
    (AudioTooLargeServiceError(),RequestEntityTooLarge),
    (ProviderNotSupportSpeechToTextServiceError(),Conflict),
    (RuntimeError('provider-private-detail'),BadGateway),
])
def test_transcription_errors_are_actionable_and_hide_provider_details(voice,cause,expected):
    _,_,invoke=voice
    invoke.side_effect=cause
    with pytest.raises(expected) as result:audio.transcribe('tenant','account','chat',object())
    assert 'provider-private-detail' not in result.value.description


def test_explicit_enabled_speech_model_does_not_replace_workspace_default(monkeypatch):
    from services import audio_service
    from graphon.model_runtime.entities.model_entities import ModelType
    from models.model import AppMode
    manager=Mock()
    manager.get_model_instance.return_value.invoke_speech2text.return_value='录音文字'
    manager.get_default_model_instance.return_value.invoke_speech2text.return_value='默认模型文字'
    monkeypatch.setattr(audio_service.ModelManager,'for_tenant',Mock(return_value=manager))
    app=SimpleNamespace(tenant_id='tenant',mode=AppMode.AGENT)
    file=FileStorage(io.BytesIO(b'wave'),filename='voice.wav',content_type='audio/wav')
    assert audio_service.AudioService._invoke_speech_to_text(app,file,'account',provider='enabled-provider',model='sensevoice')=={'text':'录音文字'}
    manager.get_model_instance.assert_called_once_with(tenant_id='tenant',provider='enabled-provider',model_type=ModelType.SPEECH2TEXT,model='sensevoice')
    manager.get_default_model_instance.assert_not_called()
    file.stream.seek(0)
    assert audio_service.AudioService._invoke_speech_to_text(app,file,'account')=={'text':'默认模型文字'}
