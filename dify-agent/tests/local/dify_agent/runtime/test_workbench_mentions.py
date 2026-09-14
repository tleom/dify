import asyncio

import httpx
import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import FunctionToolResultEvent, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.tools import DeferredToolRequests

from dify_agent.layers.dify_plugin.configs import DifyPluginToolConfig, DifyPluginToolsLayerConfig
from dify_agent.layers.dify_plugin.llm_layer import DifyPluginLLMLayer
from dify_agent.layers.dify_plugin.tools_layer import DifyPluginToolsLayer
from dify_agent.layers.workbench_mentions import (
    RequiredToolGroup,
    WorkbenchMentionsConfig,
    WorkbenchMentionsLayer,
    WorkbenchMentionsState,
)
from dify_agent.protocol.schemas import PydanticAIStreamRunEvent, RunLayerSpec, RunSucceededEvent
from dify_agent.runtime.event_sink import InMemoryRunEventSink
from dify_agent.runtime.runner import AgentRunRunner

from .test_runner import _request


def _config(run_id="turn-1"):
    return WorkbenchMentionsConfig(
        workbench_run_id=run_id,
        tool_groups=[
            RequiredToolGroup(name="chosen-plugin", tool_names=["required_tool", "alternative_tool"]),
        ],
    )


def _layer():
    layer = WorkbenchMentionsLayer.from_config(_config())
    layer.runtime_state = WorkbenchMentionsState(workbench_run_id="turn-1")
    return layer


@pytest.mark.parametrize("coalesce", [False, True])
def test_actual_runner_requires_execution_and_hides_rejected_stream(monkeypatch, coalesce):
    requests = 0
    calls = []

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield "skipped the required plugin"
        elif requests == 2:
            yield {0: DeltaToolCall(name="prepare_attachment", json_args="{}")}
        elif requests == 3:
            yield {0: DeltaToolCall(name="required_tool", json_args="{}")}
        else:
            yield "answer after actual tool execution"

    monkeypatch.setattr(DifyPluginLLMLayer, "get_model", lambda *args, **kwargs: FunctionModel(stream_function=stream))

    async def get_tools(*args, **kwargs):
        async def prepare_attachment():
            calls.append("prepare")
            return "attachment ready"

        async def required_tool():
            calls.append("required")
            return "tool observation"

        return [Tool(prepare_attachment), Tool(required_tool)]

    monkeypatch.setattr(DifyPluginToolsLayer, "get_tools", get_tools)
    request = _request(include_history=True)
    request.composition.layers.extend(
        [
            RunLayerSpec(
                name="tools",
                type="dify.plugin.tools",
                deps={"execution_context": "execution_context"},
                config=DifyPluginToolsLayerConfig(
                    tools=[
                        DifyPluginToolConfig(
                            plugin_id="test/tools",
                            provider="test",
                            tool_name="required_tool",
                            credential_type="unauthorized",
                        )
                    ]
                ),
            ),
            RunLayerSpec(name="workbench_mentions", type="dify.workbench_mentions", config=_config()),
        ]
    )
    sink = InMemoryRunEventSink()

    async def run_once(request, run_id):
        async with httpx.AsyncClient() as client:
            return await AgentRunRunner(
                sink=sink,
                request=request,
                run_id=run_id,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
                stream_text_delta_coalescing_enabled=coalesce,
            ).run()

    asyncio.run(run_once(request, "first"))
    events = sink.events["first"]
    streamed = [event for event in events if isinstance(event, PydanticAIStreamRunEvent)]
    assert calls == ["prepare", "required"]
    assert requests == 4
    assert "".join(event.agent_message_delta or "" for event in streamed) == "answer after actual tool execution"
    assert isinstance(events[-1], RunSucceededEvent)
    outcome = events[-1].data
    state = next(layer.runtime_state for layer in outcome.session_snapshot.layers if layer.name == "workbench_mentions")
    assert state["completed_tool_names"] == ["required_tool"]

    # Re-enter the same compositor snapshot: completed requirements survive.
    request.session_snapshot = outcome.session_snapshot
    asyncio.run(run_once(request, "continuation"))
    assert calls == ["prepare", "required"]
    # The next turn cannot count the old turn's calls, even if supplied its history.
    request.composition.layers[-1].config = _config("turn-2")
    request.rebuild_layers = True
    with pytest.raises(UnexpectedModelBehavior, match="retries"):
        asyncio.run(run_once(request, "new-turn"))
    assert sink.statuses["new-turn"] == "failed"


def test_argument_retries_and_unrelated_calls_do_not_fulfil_a_mention():
    layer = _layer()
    layer.record_event(FunctionToolResultEvent(RetryPromptPart(content="Missing query", tool_name="required_tool")))
    layer.record_event(FunctionToolResultEvent(ToolReturnPart(tool_name="unrelated", content="done")))
    assert layer.missing_groups == ["chosen-plugin"]
    layer.record_event(FunctionToolResultEvent(ToolReturnPart(tool_name="alternative_tool", content="Access failed")))
    assert layer.missing_groups == []


@pytest.mark.parametrize("tool_name", ["ask_human", "update_shared_environment"])
def test_human_and_environment_can_suspend_before_required_tools(tool_name):
    layer = _layer()

    def deferred(reason: str):
        raise AssertionError("Deferred tool must not execute here")

    def prepare(ctx, definition):
        definition.kind = "external"
        return definition

    model = FunctionModel(
        lambda messages, info: ModelResponse(parts=[ToolCallPart(tool_name, {"reason": "Need input"})])
    )
    agent = Agent(
        model, tools=[Tool(deferred, name=tool_name, prepare=prepare)], output_type=[str, DeferredToolRequests]
    )
    layer.require_before_answer(agent)
    result = asyncio.run(agent.run("Use the named resource"))
    assert isinstance(result.output, DeferredToolRequests)
    assert result.output.calls[0].tool_name == tool_name
    assert layer.missing_groups == ["chosen-plugin"]


def test_new_run_id_resets_state_on_resume():
    layer = _layer()
    layer.runtime_state.completed_tool_names = ["required_tool"]
    layer.config = _config("turn-2")
    asyncio.run(layer.on_context_resume())
    assert layer.missing_groups == ["chosen-plugin"]
    assert layer.runtime_state.workbench_run_id == "turn-2"
