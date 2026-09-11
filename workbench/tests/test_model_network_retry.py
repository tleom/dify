"""Fault injection at the model boundary; no provider or user task is modified."""

import json
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from core.plugin.impl.exc import PluginInvokeError
from graphon.model_runtime.entities.llm_entities import (
    LLMResultChunk,
    LLMResultChunkDelta,
)
from graphon.model_runtime.entities.message_entities import (
    AssistantPromptMessage,
    UserPromptMessage,
)
from graphon.model_runtime.errors.invoke import (
    InvokeAuthorizationError,
    InvokeBadRequestError,
    InvokeConnectionError,
    InvokeRateLimitError,
    InvokeServerUnavailableError,
)
from services import agent_llm_inner_service as gateway
from services.entities.agent_llm_inner import (
    AgentLLMInvokeCaller,
    AgentLLMInvokeRequest,
    AgentLLMInvokeTarget,
)


@pytest.fixture
def setup(monkeypatch):
    delays = []
    monkeypatch.setattr(gateway.time, "sleep", delays.append)
    request = AgentLLMInvokeRequest(
        caller=AgentLLMInvokeCaller(
            invocation_id=str(uuid4()),
            agent_run_id=str(uuid4()),
            call_index=2,
            tenant_id=str(uuid4()),
            user_id=str(uuid4()),
            user_from="account",
            app_id=str(uuid4()),
            invoke_from="debugger",
            agent_mode="workflow_run",
            agent_config_version_kind="draft",
            trace_id="network-test",
        ),
        target=AgentLLMInvokeTarget(
            provider="test",
            model="test",
            prompt_messages=[
                UserPromptMessage(content="hello").model_dump(mode="json")
            ],
        ),
    )
    model = MagicMock()
    prepared = gateway.PreparedAgentLLMInvocation(request=request, model_instance=model)
    return gateway.AgentLLMInnerService(), prepared, model, delays


def chunk():
    return LLMResultChunk(
        model="test",
        delta=LLMResultChunkDelta(
            index=0,
            message=AssistantPromptMessage(content="recovered", tool_calls=[]),
        ),
    )


@pytest.mark.parametrize(
    "error",
    [
        InvokeConnectionError("DNS failure"),
        InvokeServerUnavailableError("unavailable"),
        InvokeRateLimitError("temporary rate limit"),
        httpx.ConnectError("DNS"),
        httpx.ReadTimeout("timeout"),
        httpx.RemoteProtocolError("connection closed"),
        PluginInvokeError(
            json.dumps({"error_type": "NameResolutionError", "message": "DNS"})
        ),
        *[
            httpx.HTTPStatusError(
                "temporary",
                request=httpx.Request("POST", "https://example.test"),
                response=httpx.Response(status),
            )
            for status in [408, 429, 502, 503, 504]
        ],
    ],
)
@pytest.mark.parametrize("lazy", [False, True])
def test_temporary_failure_recovers_and_preserves_run(setup, error, lazy):
    service, prepared, model, delays = setup
    calls = []
    expected = chunk()

    def invoke(**kwargs):
        calls.append(kwargs["request_metadata"])
        failed = len(calls) < 3
        if failed and not lazy:
            raise error

        def stream():
            if failed:
                raise error
            yield expected

        return stream()

    model.invoke_llm.side_effect = invoke
    assert list(service.invoke(prepared)) == [expected]
    assert delays == [2, 4]
    assert len({metadata["invocation_id"] for metadata in calls}) == 3
    assert calls[0]["invocation_id"] == prepared.request.caller.invocation_id
    for metadata in calls:
        assert metadata["agent_run_id"] == prepared.request.caller.agent_run_id
        assert metadata["call_index"] == 2
        assert metadata["trace_id"] == "network-test"


@pytest.mark.parametrize(
    "error",
    [
        InvokeAuthorizationError("bad key"),
        InvokeBadRequestError("invalid"),
        ValueError("invalid"),
        PluginInvokeError("invalid"),
        httpx.HTTPStatusError(
            "auth",
            request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(401),
        ),
    ],
)
def test_permanent_failure_is_not_retried(setup, error):
    service, prepared, model, delays = setup
    model.invoke_llm.side_effect = error
    with pytest.raises(type(error)):
        list(service.invoke(prepared))
    assert model.invoke_llm.call_count == 1
    assert delays == []


def test_retry_budget_is_bounded(setup):
    service, prepared, model, delays = setup
    model.invoke_llm.side_effect = InvokeConnectionError("DNS")
    with pytest.raises(InvokeConnectionError):
        list(service.invoke(prepared))
    assert model.invoke_llm.call_count == 4
    assert delays == [2, 4, 8]


def test_failure_after_delivery_never_replays_output(setup):
    service, prepared, model, delays = setup
    expected = chunk()

    def stream():
        yield expected
        raise InvokeConnectionError("stream lost")

    model.invoke_llm.return_value = stream()
    output = service.invoke(prepared)
    assert next(output) == expected
    with pytest.raises(InvokeConnectionError):
        next(output)
    assert model.invoke_llm.call_count == 1
    assert delays == []


def test_consumer_close_releases_provider_generator(setup):
    service, prepared, model, delays = setup
    closed = []

    def stream():
        try:
            yield chunk()
            yield chunk()
        finally:
            closed.append(True)

    model.invoke_llm.return_value = stream()
    output = service.invoke(prepared)
    next(output)
    output.close()
    assert closed == [True]
    assert model.invoke_llm.call_count == 1
    assert delays == []


def test_cancel_during_backoff_does_not_start_another_request(setup, monkeypatch):
    service, prepared, model, _ = setup
    model.invoke_llm.side_effect = InvokeConnectionError("DNS")

    def cancel(delay):
        raise GeneratorExit

    monkeypatch.setattr(gateway.time, "sleep", cancel)
    with pytest.raises(GeneratorExit):
        list(service.invoke(prepared))
    assert model.invoke_llm.call_count == 1
