"""Voice composition uses the workspace's existing STT model, outside Agent task admission."""

from werkzeug.exceptions import BadGateway, BadRequest, Conflict, RequestEntityTooLarge

from configs import dify_config
from core.db.session_factory import session_factory
from models.model import App
from services.audio_service import AudioService
from services.errors.audio import (
    AudioTooLargeServiceError,
    NoAudioUploadedServiceError,
    ProviderNotSupportSpeechToTextServiceError,
    UnsupportedAudioTypeServiceError,
)
from services.workbench.service import _chat, authorize


def transcribe(tenant_id, account_id, chat_id, file):
    authorize(tenant_id, account_id)
    with session_factory.create_session() as session:
        chat = _chat(session, tenant_id, account_id, chat_id)
        app = session.get(App, chat.app_id)
        if app is None or app.tenant_id != tenant_id:
            raise Conflict("会话所属应用已不可用")
    # Composition is a workbench feature, independent of the published Agent's
    # tools. Reuse Dify's audio validation and configured model, with no DB lease
    # held during the remote transcription and no retained audio file.
    try:
        return AudioService._invoke_speech_to_text(
            app_model=app,
            file=file,
            end_user=account_id,
            provider=dify_config.WORKBENCH_SPEECH_PROVIDER or None,
            model=dify_config.WORKBENCH_SPEECH_MODEL or None,
        )
    except NoAudioUploadedServiceError as exc:
        raise BadRequest("请先录制语音") from exc
    except UnsupportedAudioTypeServiceError as exc:
        raise BadRequest("此录音格式暂不支持") from exc
    except AudioTooLargeServiceError as exc:
        raise RequestEntityTooLarge("录音文件过大，请缩短录音") from exc
    except ProviderNotSupportSpeechToTextServiceError as exc:
        raise Conflict("管理员尚未配置语音识别模型") from exc
    except Exception as exc:
        raise BadGateway("语音识别暂时失败，请重试或输入文字") from exc
