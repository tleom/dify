"""Personal MCP tools resolved from the current owner's frozen run declarations."""

import json
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Self

import httpx
from pydantic_ai import RunContext, Tool
from pydantic_ai.messages import ToolReturn
from pydantic_ai.tools import ToolDefinition

from agenton.layers import LayerConfig, LayerDeps, PlainLayer
from dify_agent.layers.execution_context.layer import DifyExecutionContextLayer
from dify_agent.layers.workbench_control import call_key


class WorkbenchMCPDeps(LayerDeps):
    execution_context: DifyExecutionContextLayer


@dataclass
class WorkbenchMCPLayer(PlainLayer[WorkbenchMCPDeps, LayerConfig]):
    type_id: ClassVar[str | None] = "dify.workbench_mcp"
    config: LayerConfig
    inner_api_url: str
    inner_api_key: str
    run_id: str = field(default="", init=False)

    @classmethod
    def from_config(cls, config: LayerConfig) -> Self:
        raise TypeError("WorkbenchMCPLayer requires server-injected inner API settings")

    async def get_tools(self, *, http_client: httpx.AsyncClient) -> list[Tool[object]]:
        context = self.deps.execution_context.config
        if not (
            self.run_id
            and context.workbench_run_id
            and context.user_from == "account"
            and context.user_id
            and context.app_id
        ):
            return []
        identity = {
            "tenant_id": context.tenant_id,
            "account_id": context.user_id,
            "app_id": context.app_id,
            "workbench_run_id": context.workbench_run_id,
            "backend_run_id": self.run_id,
        }
        response = await http_client.post(
            self.inner_api_url.rstrip("/") + "/inner/api/agent/workbench/mcp",
            headers={"X-Inner-Api-Key": self.inner_api_key},
            json={**identity, "operation": "list"},
            timeout=90,
        )
        response.raise_for_status()
        return [self.build_tool(item, identity, http_client) for item in response.json()["tools"]]

    def build_tool(
        self, item: dict[str, Any], identity: dict[str, Any], http_client: httpx.AsyncClient
    ) -> Tool[object]:
        async def invoke(ctx: RunContext[object], **arguments: object) -> ToolReturn:
            try:
                response = await http_client.post(
                    self.inner_api_url.rstrip("/") + "/inner/api/agent/workbench/mcp",
                    headers={"X-Inner-Api-Key": self.inner_api_key},
                    json={
                        **identity,
                        "operation": "call",
                        "tool_id": item["id"],
                        "arguments": arguments,
                        "request_key": call_key(ctx),
                    },
                    timeout=360,
                )
                response.raise_for_status()
                output = response.json()
            except httpx.HTTPStatusError as error:
                message = (
                    "个人 MCP 已停用、配置改变或当前执行已失效。"
                    if error.response.status_code in {400, 403, 409, 422}
                    else "MCP 服务响应失败，执行结果未知；请先核实外部状态，不要自动重试。"
                )
                return ToolReturn(return_value=message, metadata={"is_error": True})
            except httpx.TransportError:
                return ToolReturn(
                    return_value="MCP 请求中断，执行结果未知；请先核实外部状态，不要自动重试。",
                    metadata={"is_error": True},
                )
            result = output.get("result") or output
            return ToolReturn(
                return_value=json.dumps(result, ensure_ascii=False),
                metadata={"is_error": bool(output.get("error") or result.get("isError"))},
            )

        async def prepare(_ctx: RunContext[object], definition: ToolDefinition) -> ToolDefinition:
            return replace(definition, parameters_json_schema=item["input_schema"], strict=False)

        return Tool(
            invoke,
            takes_ctx=True,
            name=item["runtime_name"],
            description=f"{item['provider_name']} / {item['tool_name']}: {item['description']}",
            prepare=prepare,
        )
