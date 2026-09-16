"""Exact-execution tools and model-boundary collaboration state."""

import hmac
from typing import Any

from dify_agent.protocol.workbench_control import WorkbenchControlState
from flask import abort, request
from flask_restx import Resource
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from werkzeug.exceptions import BadRequest

from configs import dify_config
from controllers.common.schema import (
    register_response_schema_models,
    register_schema_models,
)
from controllers.inner_api import inner_api_ns
from controllers.inner_api.wraps import plugin_inner_api_only
from fields.base import ResponseModel
from libs.helper import dump_response
from services.workbench.control import AgentControlPayload, agent_control
from services.workbench.personal_mcp import (
    AgentMCPPayload,
    MCPAuthorizationPayload,
    authorize_call,
)
from services.workbench.personal_mcp import agent_operation as mcp_operation
from services.workbench.planning import AgentPlanInspectPayload, inspect
from services.workbench.resources import AgentMemoryPayload, agent_memory_update


class AgentControlResponse(ResponseModel):
    state: WorkbenchControlState
    control: dict | None = None


class AgentResourcesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    account_id: str
    app_id: str
    workbench_run_id: str
    backend_run_id: str
    initialize: bool = False


class AgentResourcesResponse(ResponseModel):
    memory: dict[str, Any]
    skills: list[dict[str, Any]]
    global_resources: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)


class AgentMemoryResponse(ResponseModel):
    content: str
    version: str | None


class PlanPreviewResponse(ResponseModel):
    path: str
    media_type: str
    data: str


class AgentPlanInspectResponse(ResponseModel):
    output: str
    output_path: str
    output_truncated: bool
    exit_code: int
    timed_out: bool
    previews: list[PlanPreviewResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AgentMCPResponse(ResponseModel):
    tools: list[dict[str, Any]] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    uncertain: bool = False


class MCPAuthorizationResponse(ResponseModel):
    authorized: bool


register_schema_models(inner_api_ns, AgentControlPayload)
register_response_schema_models(inner_api_ns, AgentControlResponse)
register_schema_models(inner_api_ns, AgentResourcesPayload)
register_response_schema_models(inner_api_ns, AgentResourcesResponse)
register_schema_models(inner_api_ns, AgentMemoryPayload)
register_response_schema_models(inner_api_ns, AgentMemoryResponse)
register_schema_models(inner_api_ns, AgentPlanInspectPayload)
register_response_schema_models(inner_api_ns, PlanPreviewResponse, AgentPlanInspectResponse)
register_schema_models(inner_api_ns, AgentMCPPayload)
register_response_schema_models(inner_api_ns, AgentMCPResponse)
register_schema_models(inner_api_ns, MCPAuthorizationPayload)
register_response_schema_models(inner_api_ns, MCPAuthorizationResponse)


@inner_api_ns.route("/agent/workbench/plan/inspect")
class AgentWorkbenchPlanInspect(Resource):
    @plugin_inner_api_only
    @inner_api_ns.expect(inner_api_ns.models[AgentPlanInspectPayload.__name__])
    @inner_api_ns.response(
        200, "Read-only planning investigation", inner_api_ns.models[AgentPlanInspectResponse.__name__]
    )
    def post(self):
        try:
            payload = AgentPlanInspectPayload.model_validate(inner_api_ns.payload or {})
        except ValidationError as error:
            raise BadRequest("计划调查参数无效") from error
        return dump_response(AgentPlanInspectResponse, inspect(payload))


@inner_api_ns.route("/agent/workbench/mcp/authorize")
class ManagerMCPAuthorization(Resource):
    @inner_api_ns.expect(inner_api_ns.models[MCPAuthorizationPayload.__name__])
    @inner_api_ns.response(
        200,
        "Current execution authorization",
        inner_api_ns.models[MCPAuthorizationResponse.__name__],
    )
    def post(self):
        token = dify_config.WORKBENCH_SANDBOX_MANAGER_TOKEN
        if not token or not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
            abort(403)
        payload = MCPAuthorizationPayload.model_validate(inner_api_ns.payload or {})
        return dump_response(MCPAuthorizationResponse, authorize_call(payload))


@inner_api_ns.route("/agent/workbench/mcp")
class AgentWorkbenchMCP(Resource):
    @plugin_inner_api_only
    @inner_api_ns.expect(inner_api_ns.models[AgentMCPPayload.__name__])
    @inner_api_ns.response(
        200,
        "Personal MCP tools or invocation result",
        inner_api_ns.models[AgentMCPResponse.__name__],
    )
    def post(self):
        payload = AgentMCPPayload.model_validate(inner_api_ns.payload or {})
        return dump_response(AgentMCPResponse, mcp_operation(payload))


@inner_api_ns.route("/agent/workbench/control")
class AgentWorkbenchControl(Resource):
    @plugin_inner_api_only
    @inner_api_ns.expect(inner_api_ns.models[AgentControlPayload.__name__])
    @inner_api_ns.response(
        200,
        "Current collaboration state",
        inner_api_ns.models[AgentControlResponse.__name__],
    )
    def post(self):
        try:
            payload = AgentControlPayload.model_validate(inner_api_ns.payload or {})
        except ValidationError as error:
            raise BadRequest("模式控制参数无效") from error
        return dump_response(AgentControlResponse, agent_control(payload))


@inner_api_ns.route("/agent/workbench/resources")
class AgentWorkbenchResources(Resource):
    @plugin_inner_api_only
    @inner_api_ns.expect(inner_api_ns.models[AgentResourcesPayload.__name__])
    @inner_api_ns.response(
        200,
        "Personal memory and available skills",
        inner_api_ns.models[AgentResourcesResponse.__name__],
    )
    def post(self):
        from services.workbench.resources import agent_snapshot

        payload = AgentResourcesPayload.model_validate(inner_api_ns.payload or {})
        result = agent_snapshot(payload, initialize=payload.initialize)
        result["global_resources"] = result.pop("global", None)
        return dump_response(AgentResourcesResponse, result)


@inner_api_ns.route("/agent/workbench/memory")
class AgentWorkbenchMemory(Resource):
    @plugin_inner_api_only
    @inner_api_ns.expect(inner_api_ns.models[AgentMemoryPayload.__name__])
    @inner_api_ns.response(
        200,
        "Updated personal memory",
        inner_api_ns.models[AgentMemoryResponse.__name__],
    )
    def post(self):
        try:
            payload = AgentMemoryPayload.model_validate(inner_api_ns.payload or {})
        except ValidationError as error:
            raise BadRequest("记忆更新参数无效") from error
        return dump_response(AgentMemoryResponse, agent_memory_update(payload))
