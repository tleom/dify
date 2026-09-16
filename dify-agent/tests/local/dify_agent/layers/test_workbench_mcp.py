"""Real compositor/tool schema wiring with an explicit fake inner API transport."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturn, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agenton.compositor import Compositor, LayerNode, LayerProvider
from agenton.layers import LayerConfig
from dify_agent.layers.execution_context import DifyExecutionContextLayerConfig
from dify_agent.layers.execution_context.layer import DifyExecutionContextLayer
from dify_agent.layers.workbench_mcp import WorkbenchMCPLayer
from dify_agent.runtime.runner import _resolve_run_tools

DECLARATION = {
    "id": "personal:mcp:demo:query",
    "runtime_name": "personal_mcp_123",
    "provider_name": "个人查询",
    "tool_name": "query",
    "description": "查询信息",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}


def compositor():
    return Compositor(
        [
            LayerNode(
                "execution_context",
                LayerProvider.from_factory(
                    layer_type=DifyExecutionContextLayer,
                    create=lambda config: DifyExecutionContextLayer.from_config_with_settings(
                        DifyExecutionContextLayerConfig.model_validate(config),
                        daemon_url="http://daemon",
                        daemon_api_key="private",
                    ),
                ),
            ),
            LayerNode(
                "mcp",
                LayerProvider.from_factory(
                    layer_type=WorkbenchMCPLayer,
                    create=lambda config: WorkbenchMCPLayer(
                        config=LayerConfig.model_validate(config), inner_api_url="http://api", inner_api_key="inner-key"
                    ),
                ),
                deps={"execution_context": "execution_context"},
            ),
        ]
    )


@pytest.mark.parametrize("is_error", [False, True])
def test_tools_preserve_schema_execution_identity_and_mcp_business_error(is_error):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.headers["X-Inner-Api-Key"] == "inner-key"
        if payload["operation"] == "list":
            return httpx.Response(200, json={"tools": [DECLARATION]})
        return httpx.Response(
            200,
            json={
                "result": {
                    "isError": is_error,
                    "content": [{"type": "text", "text": "结果"}],
                    "structuredContent": {"count": 2},
                }
            },
        )

    async def run():
        async with (
            compositor().enter(
                configs={
                    "execution_context": DifyExecutionContextLayerConfig(
                        tenant_id="tenant",
                        user_id="owner",
                        user_from="account",
                        app_id="app",
                        workbench_run_id="run",
                        agent_mode="agent_app",
                        invoke_from="web-app",
                    ),
                    "mcp": LayerConfig(),
                }
            ) as context,
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        ):
            layer = context.get_layer("mcp", WorkbenchMCPLayer)
            assert await layer.get_tools(http_client=client) == []
            layer.run_id = "execution"
            tool = (await layer.get_tools(http_client=client))[0]
            definition = await tool.prepare_tool_def(None)
            assert definition.parameters_json_schema == DECLARATION["input_schema"]
            ctx = SimpleNamespace(run_step=1, tool_name=tool.name, tool_call_id="call-1")
            result = await tool.function_schema.call({"query": "甲"}, ctx)
            assert isinstance(result, ToolReturn) and result.metadata["is_error"] is is_error
            assert json.loads(result.return_value)["structuredContent"] == {"count": 2}

    asyncio.run(run())
    assert len(requests) == 2
    assert requests[1]["backend_run_id"] == "execution" and requests[1]["account_id"] == "owner"
    assert requests[1]["arguments"] == {"query": "甲"}
    assert requests[1]["request_key"] and "inner-key" not in json.dumps(requests)


def test_transport_failure_is_reported_once_without_replaying_call():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("may have executed", request=request)

    async def run():
        layer = WorkbenchMCPLayer(config=LayerConfig(), inner_api_url="http://api", inner_api_key="key")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tool = layer.build_tool(DECLARATION, {"backend_run_id": "execution"}, client)
            result = await tool.function_schema.call(
                {"query": "甲"}, SimpleNamespace(run_step=1, tool_name=tool.name, tool_call_id="call")
            )
            assert result.metadata["is_error"] is True
            assert "结果未知" in result.return_value

    asyncio.run(run())
    assert len(calls) == 1


def test_runner_resolves_personal_tools_and_model_executes_declared_schema():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        if body["operation"] == "list":
            return httpx.Response(200, json={"tools": [DECLARATION]})
        calls.append(body)
        return httpx.Response(
            200, json={"result": {"content": [], "structuredContent": {"count": 3}, "isError": False}}
        )

    async def model(messages, info):
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if returned:
            assert json.loads(returned[-1].content)["structuredContent"] == {"count": 3}
            return ModelResponse(parts=[TextPart("已查询")])
        definition = next(tool for tool in info.function_tools if tool.name == DECLARATION["runtime_name"])
        assert definition.parameters_json_schema == DECLARATION["input_schema"]
        return ModelResponse(
            parts=[ToolCallPart(definition.name, {"query": "真实框架调用"}, tool_call_id="model-call")]
        )

    async def run():
        async with (
            compositor().enter(
                configs={
                    "execution_context": DifyExecutionContextLayerConfig(
                        tenant_id="tenant",
                        user_id="owner",
                        user_from="account",
                        app_id="app",
                        workbench_run_id="run",
                        agent_mode="agent_app",
                        invoke_from="web-app",
                    ),
                    "mcp": LayerConfig(),
                }
            ) as context,
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        ):
            context.get_layer("mcp", WorkbenchMCPLayer).run_id = "execution"
            tools = await _resolve_run_tools(context, plugin_daemon_http_client=client, dify_api_http_client=client)
            result = await Agent(FunctionModel(model), tools=tools).run("查询")
            assert result.output == "已查询"

    asyncio.run(run())
    assert len(calls) == 1
    assert calls[0]["arguments"] == {"query": "真实框架调用"}
