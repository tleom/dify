"""The Agent obtains the same verified links as the authenticated file space."""

from flask_restx import Resource
from pydantic import ValidationError
from werkzeug.exceptions import BadRequest

from controllers.common.schema import register_response_schema_models, register_schema_models
from controllers.inner_api import inner_api_ns
from controllers.inner_api.wraps import plugin_inner_api_only
from fields.workbench_file_fields import AgentWorkbenchFilesResponse, WorkbenchFilePreviewResponse
from libs.helper import dump_response
from services.workbench.file_links import AgentFileLinksPayload, agent_lookup
from services.workbench.preview import AgentFilePreviewPayload, open_preview

register_schema_models(inner_api_ns, AgentFileLinksPayload, AgentFilePreviewPayload)
register_response_schema_models(inner_api_ns, AgentWorkbenchFilesResponse, WorkbenchFilePreviewResponse)


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


@inner_api_ns.route("/agent/workbench/files/preview")
class AgentWorkbenchFilePreview(Resource):
    @plugin_inner_api_only
    @inner_api_ns.expect(inner_api_ns.models[AgentFilePreviewPayload.__name__])
    @inner_api_ns.response(200, "Sidebar preview requested", inner_api_ns.models[WorkbenchFilePreviewResponse.__name__])
    def post(self):
        try:
            payload = AgentFilePreviewPayload.model_validate(inner_api_ns.payload or {})
        except ValidationError as error:
            raise BadRequest("文件预览参数无效") from error
        return dump_response(WorkbenchFilePreviewResponse, open_preview(payload))
