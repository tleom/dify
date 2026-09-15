"""Deliver persisted follow-ups to the exact active execution ticket."""

from flask_restx import Resource
from pydantic import Field, ValidationError
from werkzeug.exceptions import BadRequest

from controllers.common.schema import register_response_schema_models, register_schema_models
from controllers.inner_api import inner_api_ns
from controllers.inner_api.wraps import plugin_inner_api_only
from fields.base import ResponseModel
from libs.helper import dump_response
from services.workbench.followups import AgentFollowupsPayload, poll


class AgentFollowupMessageResponse(ResponseModel):
    id: str
    content: str


class AgentFollowupsResponse(ResponseModel):
    messages: list[AgentFollowupMessageResponse] = Field(default_factory=list)
    sealed: bool


register_schema_models(inner_api_ns, AgentFollowupsPayload)
register_response_schema_models(inner_api_ns, AgentFollowupsResponse)


@inner_api_ns.route("/agent/workbench/followups")
class AgentWorkbenchFollowups(Resource):
    @plugin_inner_api_only
    @inner_api_ns.expect(inner_api_ns.models[AgentFollowupsPayload.__name__])
    @inner_api_ns.response(200, "Current task follow-ups", inner_api_ns.models[AgentFollowupsResponse.__name__])
    def post(self):
        try:
            payload = AgentFollowupsPayload.model_validate(inner_api_ns.payload or {})
        except ValidationError as error:
            raise BadRequest("跟进消息参数无效") from error
        return dump_response(AgentFollowupsResponse, poll(payload))
