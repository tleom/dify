"""Owner-scoped, file-backed MCP resources and exact-execution invocation."""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from werkzeug.exceptions import BadRequest, Conflict, Forbidden

from core.db.session_factory import session_factory
from models.agent import AgentWorkingResourceStatus, AgentWorkspaceBinding
from models.model import Conversation
from models.workbench import WorkbenchRun
from services.workbench.files import ensure_workspace, manager

PREFIX = "personal:mcp:"
PUBLIC_OPERATIONS = {
    "mcp_read",
    "mcp_save",
    "mcp_delete",
    "mcp_toggle",
    "mcp_pin",
    "mcp_test",
}


class AgentMCPPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    account_id: str
    app_id: str
    workbench_run_id: str
    backend_run_id: str
    operation: Literal["list", "call"] = "list"
    tool_id: str | None = Field(default=None, max_length=260)
    arguments: dict[str, Any] = Field(default_factory=dict)
    request_key: str | None = Field(default=None, max_length=160)


class MCPAuthorizationPayload(AgentMCPPayload):
    workspace_id: str
    binding_id: str
    version: str
    manifest_version: str


def execution_context(payload: AgentMCPPayload, workspace_id: str):
    """Read the current execution and its server-owned binding in one short transaction."""
    from services.workbench import control
    from services.workbench.recovery import locked_run

    with session_factory.get_session_maker().begin() as session:
        chat, run = locked_run(session, payload.tenant_id, payload.account_id, payload.workbench_run_id)
        if chat.app_id != payload.app_id or run.status != "running" or run.backend_run_id != payload.backend_run_id:
            raise Forbidden()
        if control.load(session, chat).plan.active:
            raise Forbidden("计划阶段不能执行个人 MCP 工具，请先提交完整计划并取得批准")
        conversation = session.get(Conversation, chat.conversation_id) if chat.conversation_id else None
        if (
            conversation is None
            or conversation.app_id != payload.app_id
            or conversation.from_account_id != payload.account_id
            or conversation.is_deleted
        ):
            raise Forbidden()
        binding = (
            session.get(AgentWorkspaceBinding, conversation.agent_workspace_binding_id)
            if conversation.agent_workspace_binding_id
            else None
        )
        if (
            binding is None
            or binding.tenant_id != payload.tenant_id
            or binding.app_id != payload.app_id
            or binding.workspace_id != workspace_id
            or binding.status != AgentWorkingResourceStatus.ACTIVE
        ):
            raise Forbidden()
        return binding.id, json.loads(run.payload).get("personal_mcp_tools", [])


def authorize_call(payload: MCPAuthorizationPayload):
    """Manager calls this after all queue/connection waits, immediately before dispatch."""
    binding, frozen = execution_context(payload, payload.workspace_id)
    if binding != payload.binding_id or not any(
        tool["id"] == payload.tool_id
        and tool["version"] == payload.version
        and tool["manifest_version"] == payload.manifest_version
        for tool in frozen
    ):
        raise Forbidden()
    return {"authorized": True}


def runtime_name(server_id: str, tool_name: str) -> str:
    return "personal_mcp_" + hashlib.sha256((server_id + "\0" + tool_name).encode()).hexdigest()[:24]


def snapshot(tenant_id, account_id):
    return manager(
        ensure_workspace(tenant_id, account_id),
        "personal-mcp",
        {"operation": "mcp_list"},
    )


def catalog(tenant_id, account_id):
    tools = []
    for server in snapshot(tenant_id, account_id)["mcp"]:
        if not server.get("enabled") or server.get("status") != "ready":
            continue
        provider = PREFIX + server["id"]
        for item in server["tools"]:
            tools.append(
                {
                    "id": provider + ":" + item["name"],
                    "scope": "personal",
                    "group": "mcp",
                    "plugin_id": provider,
                    "provider": provider,
                    "provider_name": server["name"],
                    "name": item["name"],
                    "tool_name": item["name"],
                    "description": item["description"],
                    "server_id": server["id"],
                    "version": server["version"],
                    "manifest_version": server["manifest_version"],
                    "runtime_name": runtime_name(server["id"], item["name"]),
                    "input_schema": item["inputSchema"],
                }
            )
    return tools


def mutate(tenant_id, account_id, payload):
    if payload.get("operation") not in PUBLIC_OPERATIONS:
        raise BadRequest("不支持的个人 MCP 操作")
    identifier = ensure_workspace(tenant_id, account_id)
    # Manager serializes short file mutations and cancels affected connections.
    result = manager(identifier, "personal-mcp", payload, timeout=45)
    if result.get("conflict"):
        raise Conflict("MCP 配置已改变，请刷新后重试")
    if result.get("error"):
        raise BadRequest(result["error"])
    return result


def run_tools(run_id, tenant_id, account_id):
    """Only server-prepared declarations frozen in this account's run are usable."""
    with session_factory.create_session() as session:
        run = session.scalar(
            select(WorkbenchRun).where(
                WorkbenchRun.id == run_id,
                WorkbenchRun.tenant_id == tenant_id,
                WorkbenchRun.account_id == account_id,
                WorkbenchRun.status == "running",
            )
        )
        if run is None:
            raise Forbidden()
        return json.loads(run.payload).get("personal_mcp_tools", [])


def available_run_tools(run_id, tenant_id, account_id):
    frozen = run_tools(run_id, tenant_id, account_id)
    if not frozen:
        return []
    current = {tool["id"]: tool for tool in catalog(tenant_id, account_id)}
    return [
        tool
        for tool in frozen
        if tool["id"] in current
        and all(tool.get(key) == current[tool["id"]].get(key) for key in ("version", "manifest_version"))
    ]


def agent_operation(payload: AgentMCPPayload):
    from services.workbench.resources import _agent_soul

    _agent_soul(payload)
    if payload.operation == "list":
        return {"tools": available_run_tools(payload.workbench_run_id, payload.tenant_id, payload.account_id)}
    if not payload.request_key or not payload.tool_id:
        raise BadRequest("MCP 调用缺少工具或请求标识")
    if len(json.dumps(payload.arguments).encode()) > 1024 * 1024:
        raise BadRequest("MCP 调用参数超过 1 MiB")
    identifier = ensure_workspace(payload.tenant_id, payload.account_id)
    allowed = available_run_tools(payload.workbench_run_id, payload.tenant_id, payload.account_id)
    tool = next((item for item in allowed if item["id"] == payload.tool_id), None)
    if tool is None:
        raise Forbidden("个人 MCP 已停用、配置已改变或不属于本轮任务")
    binding, _ = execution_context(payload, identifier)
    authorization = {
        **payload.model_dump(exclude={"arguments", "request_key"}),
        "workspace_id": identifier,
        "binding_id": binding,
        "version": tool["version"],
        "manifest_version": tool["manifest_version"],
    }
    result = manager(
        identifier,
        "personal-mcp",
        {
            "operation": "mcp_call",
            "name": tool["server_id"],
            "version": tool["version"],
            "manifest_version": tool["manifest_version"],
            "tool": tool["tool_name"],
            "arguments": payload.arguments,
            "request_key": f"{payload.workbench_run_id}:{payload.backend_run_id}:{payload.request_key}",
            "authorization": authorization,
        },
        timeout=350,
    )
    if result.get("conflict"):
        return {"error": "MCP 配置已改变，请刷新插件后开始新一轮任务"}
    return result
