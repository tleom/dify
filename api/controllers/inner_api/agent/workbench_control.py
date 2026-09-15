"""Exact-execution tools and model-boundary collaboration state."""

from typing import Any

from dify_agent.protocol.workbench_control import WorkbenchControlState
from flask_restx import Resource
from pydantic import BaseModel, ConfigDict, ValidationError
from werkzeug.exceptions import BadRequest

from controllers.common.schema import register_response_schema_models, register_schema_models
from controllers.inner_api import inner_api_ns
from controllers.inner_api.wraps import plugin_inner_api_only
from fields.base import ResponseModel
from libs.helper import dump_response
from services.workbench.control import AgentControlPayload, agent_control


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


register_schema_models(inner_api_ns, AgentControlPayload)
register_response_schema_models(inner_api_ns, AgentControlResponse)
register_schema_models(inner_api_ns, AgentResourcesPayload)
register_response_schema_models(inner_api_ns, AgentResourcesResponse)


@inner_api_ns.route("/agent/workbench/control")
class AgentWorkbenchControl(Resource):
    @plugin_inner_api_only
    @inner_api_ns.expect(inner_api_ns.models[AgentControlPayload.__name__])
    @inner_api_ns.response(200, "Current collaboration state", inner_api_ns.models[AgentControlResponse.__name__])
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
        200, "Personal memory and available skills", inner_api_ns.models[AgentResourcesResponse.__name__]
    )
    def post(self):
        from services.workbench.resources import agent_snapshot

        payload = AgentResourcesPayload.model_validate(inner_api_ns.payload or {})
        result = agent_snapshot(payload, initialize=payload.initialize)
        result["global_resources"] = result.pop("global", None)
        return dump_response(AgentResourcesResponse, result)
