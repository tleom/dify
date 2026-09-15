"""The Agent obtains the same verified links as the authenticated file space."""

from flask_restx import Resource
from pydantic import ValidationError
from werkzeug.exceptions import BadRequest

from controllers.common.schema import register_response_schema_models, register_schema_models
from controllers.inner_api import inner_api_ns
from controllers.inner_api.wraps import plugin_inner_api_only
from fields.workbench_file_fields import AgentWorkbenchFilesResponse
from libs.helper import dump_response
from services.workbench.file_links import AgentFileLinksPayload, agent_lookup

register_schema_models(inner_api_ns, AgentFileLinksPayload)
register_response_schema_models(inner_api_ns, AgentWorkbenchFilesResponse)


@inner_api_ns.route("/agent/workbench/files")
class AgentWorkbenchFiles(Resource):
    @plugin_inner_api_only
    @inner_api_ns.expect(inner_api_ns.models[AgentFileLinksPayload.__name__])
    @inner_api_ns.response(200, "Verified file-space links", inner_api_ns.models[AgentWorkbenchFilesResponse.__name__])
    def post(self):
        try:
            payload = AgentFileLinksPayload.model_validate(inner_api_ns.payload or {})
        except ValidationError as error:
            raise BadRequest("文件查询参数无效") from error
        return dump_response(AgentWorkbenchFilesResponse, agent_lookup(payload))
