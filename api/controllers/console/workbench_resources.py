"""User-owned resources; administrator catalogs have no mutation surface here."""

from typing import Any

from flask import request
from pydantic import BaseModel, Field

from controllers.common.schema import register_response_schema_models, register_schema_models
from controllers.console import console_ns
from controllers.console.workbench import WorkbenchFileLinksQuery, WorkbenchResource
from fields.base import ResponseModel
from libs.helper import dump_response
from services.workbench import resources


class ResourcesResponse(ResponseModel):
    data: dict[str, Any]


class DirectoryPayload(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


register_schema_models(console_ns, resources.ResourceMutation)
register_schema_models(console_ns, DirectoryPayload)
register_response_schema_models(console_ns, ResourcesResponse)


@console_ns.route("/workbench/resources")
class Resources(WorkbenchResource):
    @console_ns.response(200, "Personal and administrator resources", console_ns.models[ResourcesResponse.__name__])
    def get(self):
        return dump_response(ResourcesResponse, {"data": resources.listing(*self.owner())})

    @console_ns.expect(console_ns.models[resources.ResourceMutation.__name__])
    @console_ns.response(200, "Updated personal resource", console_ns.models[ResourcesResponse.__name__])
    def post(self):
        payload = resources.ResourceMutation.model_validate(console_ns.payload or {})
        return dump_response(
            ResourcesResponse, {"data": resources.mutate(*self.owner(), payload.model_dump(mode="json"))}
        )


@console_ns.route("/workbench/files/directories")
class Directories(WorkbenchResource):
    @console_ns.expect(console_ns.models[DirectoryPayload.__name__])
    @console_ns.response(200, "Created personal directory", console_ns.models[ResourcesResponse.__name__])
    def post(self):
        from services.workbench.files import operate

        payload = DirectoryPayload.model_validate(console_ns.payload or {})
        return dump_response(ResourcesResponse, {"data": operate(*self.owner(), "mkdir", payload.path)})


@console_ns.route("/workbench/files/stat")
class FileStat(WorkbenchResource):
    @console_ns.response(200, "Version checked metadata", console_ns.models[ResourcesResponse.__name__])
    def get(self):
        from services.workbench.file_links import lookup

        query = WorkbenchFileLinksQuery.model_validate(request.args.to_dict())
        return dump_response(ResourcesResponse, {"data": lookup(*self.owner(), query.path, require_downloadable=False)})
