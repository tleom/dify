"""API-owned model invocation for dify-agent."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator
from contextlib import closing
from dataclasses import dataclass
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import httpx
from sqlalchemy.orm import Session

from core.app.entities.app_invoke_entities import get_credit_usage_app_type
from core.credit_usage import CreditUsageAppType, CreditUsageCreatedBy
from core.db.session_factory import session_factory as default_session_factory
from core.entities.model_entities import ModelStatus
from core.model_manager import ModelInstance, ModelManager
from core.plugin.impl.exc import PluginInvokeError
from core.plugin.impl.model_runtime_factory import create_plugin_provider_manager
from graphon.model_runtime.entities.llm_entities import LLMResultChunk
from graphon.model_runtime.entities.message_entities import PromptMessage
from graphon.model_runtime.entities.model_entities import ModelType
from graphon.model_runtime.errors.invoke import (
    InvokeConnectionError,
    InvokeRateLimitError,
    InvokeServerUnavailableError,
)
from models.model import App
from services.entities.agent_llm_inner import AgentLLMInvokeRequest

logger = logging.getLogger(__name__)
MODEL_RETRY_DELAYS = (2, 4, 8)


def _temporary_model_error(error: Exception) -> bool:
    if isinstance(error, (InvokeConnectionError, InvokeServerUnavailableError, InvokeRateLimitError)):
        return True
    if isinstance(error, PluginInvokeError):
        return error.get_error_type() in {
            "InvokeConnectionError",
            "InvokeServerUnavailableError",
            "InvokeRateLimitError",
            "ConnectTimeout",
            "ReadTimeout",
            "ConnectionError",
            "NameResolutionError",
        }
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 429, 502, 503, 504}
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


class AgentLLMInnerServiceError(RuntimeError):
    def __init__(self, error_code: str, description: str, *, status_code: int = 500) -> None:
        self.error_code = error_code
        self.description = description
        self.status_code = status_code
        super().__init__(description)


@dataclass(frozen=True)
class PreparedAgentLLMInvocation:
    request: AgentLLMInvokeRequest
    model_instance: ModelInstance
    app_type: CreditUsageAppType = CreditUsageAppType.UNKNOWN

    @property
    def created_by(self) -> CreditUsageCreatedBy:
        if self.request.caller.agent_config_version_kind == "build_draft":
            return CreditUsageCreatedBy.BUILD_DRAFT
        if self.app_type is CreditUsageAppType.AGENT_V2:
            return CreditUsageCreatedBy.APP
        return CreditUsageCreatedBy.AGENT_NODE


class AgentLLMInnerService:
    """Resolve the model and invoke it through the API model runtime."""

    def __init__(self, *, session_factory: Callable[[], Session] | None = None) -> None:
        self._session_factory = session_factory or default_session_factory.create_session

    def prepare(self, request: AgentLLMInvokeRequest) -> PreparedAgentLLMInvocation:
        caller = request.caller
        target = request.target
        app = self._validate_app_tenant(app_id=caller.app_id, tenant_id=caller.tenant_id)
        provider_manager = create_plugin_provider_manager(tenant_id=caller.tenant_id, user_id=caller.user_id)
        model_manager = ModelManager(provider_manager=provider_manager)
        model_instance = model_manager.get_model_instance(
            tenant_id=caller.tenant_id,
            provider=target.provider,
            model_type=ModelType.LLM,
            model=target.model,
        )

        provider_configuration = model_instance.provider_model_bundle.configuration
        provider_model = provider_configuration.get_provider_model(model_type=ModelType.LLM, model=target.model)
        if provider_model is None:
            raise AgentLLMInnerServiceError(
                "model_not_found",
                f"Model {target.model} does not exist for provider {target.provider}.",
                status_code=404,
            )
        # The cached model status can lag behind the authoritative reservation.
        # Preserve other provider errors, but let quota reservation produce 429.
        if provider_model.status != ModelStatus.QUOTA_EXCEEDED:
            provider_model.raise_for_status()

        return PreparedAgentLLMInvocation(
            request=request,
            model_instance=model_instance,
            app_type=get_credit_usage_app_type(app.mode),
        )

    def invoke(self, prepared: PreparedAgentLLMInvocation) -> Generator[LLMResultChunk, None, None]:
        """Retry a failed model request before delivery, preserving the surrounding Agent run.

        Once any chunk has been delivered the model stream cannot be replayed
        safely: the provider has no cursor and may generate different output.
        Agent event-stream reconnects are separately handled with event cursors.
        """
        for attempt in range(len(MODEL_RETRY_DELAYS) + 1):
            delivered = False
            try:
                with closing(self._invoke_once(prepared, retry_attempt=attempt)) as chunks:
                    for chunk in chunks:
                        delivered = True
                        yield chunk
                return
            except Exception as error:
                if delivered or attempt == len(MODEL_RETRY_DELAYS) or not _temporary_model_error(error):
                    raise
                delay = MODEL_RETRY_DELAYS[attempt]
                logger.warning(
                    "Temporary Agent model error; retry %s/%s in %ss (%s)",
                    attempt + 1,
                    len(MODEL_RETRY_DELAYS),
                    delay,
                    type(error).__name__,
                )
                time.sleep(delay)

    def _invoke_once(
        self, prepared: PreparedAgentLLMInvocation, *, retry_attempt: int
    ) -> Generator[LLMResultChunk, None, None]:
        request = prepared.request
        caller = request.caller
        target = request.target
        result = prepared.model_instance.invoke_llm(
            prompt_messages=cast(list[PromptMessage], target.prompt_messages),
            model_parameters=target.model_parameters,
            tools=target.tools,
            stop=target.stop,
            # The gateway transport is always streamed, including Pydantic AI's
            # non-streaming request path, so one response protocol is sufficient.
            stream=True,
            request_metadata={
                "source": "agent_llm_gateway",
                # Failed reservations are released by ModelInstance. A distinct
                # attempt key avoids reusing a released hosted billing reservation.
                "invocation_id": caller.invocation_id
                if retry_attempt == 0
                else str(uuid5(NAMESPACE_URL, f"{caller.invocation_id}:retry:{retry_attempt}")),
                "agent_run_id": caller.agent_run_id,
                "agent_mode": caller.agent_mode,
                "agent_config_version_kind": caller.agent_config_version_kind,
                "call_index": caller.call_index,
                "app_id": caller.app_id,
                "workflow_run_id": caller.workflow_run_id,
                "node_execution_id": caller.node_execution_id,
                "trace_id": caller.trace_id,
                "app_type": prepared.app_type,
                "created_by": prepared.created_by,
            },
        )
        yield from cast(Generator[LLMResultChunk, None, None], result)

    def _validate_app_tenant(self, *, app_id: str, tenant_id: str) -> App:
        with self._session_factory() as session:
            app = session.get(App, app_id)
            if app is None:
                raise AgentLLMInnerServiceError(
                    "app_not_found",
                    "App not found.",
                    status_code=404,
                )
            if app.tenant_id != tenant_id:
                raise AgentLLMInnerServiceError(
                    "app_tenant_mismatch",
                    "App does not belong to the caller tenant.",
                    status_code=403,
                )
            return app


__all__ = ["AgentLLMInnerService", "AgentLLMInnerServiceError", "PreparedAgentLLMInvocation"]
