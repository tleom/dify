"""Authenticated commands and collaboration-state observation for the workbench."""

from dify_agent.protocol.workbench_control import WorkbenchControlState
from pydantic import BaseModel, ConfigDict, Field

from controllers.common.schema import register_response_schema_models, register_schema_models
from controllers.console import console_ns
from controllers.console.workbench import WorkbenchResource, WorkbenchRunResponse, WorkbenchSandboxFilePayload
from fields.base import ResponseModel
from libs.helper import dump_response
from services.workbench import control
from services.workbench.mentions import ResourceMentions


class WorkbenchCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=100000)
    request_key: str = Field(min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=0)
    files: list[WorkbenchSandboxFilePayload] = Field(default_factory=list, max_length=20)
    resource_mentions: ResourceMentions | None = None


class WorkbenchControlResponse(ResponseModel):
    data: WorkbenchControlState


class WorkbenchClearTodosPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)


class WorkbenchCommandResultResponse(ResponseModel):
    state: WorkbenchControlState
    message: str
    run: WorkbenchRunResponse | None = None


class WorkbenchCommandResponse(ResponseModel):
    data: WorkbenchCommandResultResponse


register_schema_models(console_ns, WorkbenchCommandPayload, WorkbenchClearTodosPayload)
register_response_schema_models(console_ns, WorkbenchControlResponse, WorkbenchCommandResponse)


@console_ns.route("/workbench/chats/<uuid:chat_id>/control")
class Control(WorkbenchResource):
    @console_ns.response(200, "Current goal, plan and task list", console_ns.models[WorkbenchControlResponse.__name__])
    def get(self, chat_id):
        return dump_response(WorkbenchControlResponse, {"data": control.read(*self.owner(), str(chat_id))})


@console_ns.route("/workbench/chats/<uuid:chat_id>/todos/clear")
class ClearTodos(WorkbenchResource):
    @console_ns.expect(console_ns.models[WorkbenchClearTodosPayload.__name__])
    @console_ns.response(200, "Task list cleared", console_ns.models[WorkbenchControlResponse.__name__])
    def post(self, chat_id):
        payload = WorkbenchClearTodosPayload.model_validate(console_ns.payload or {})
        return dump_response(
            WorkbenchControlResponse,
            {"data": control.clear_todos(*self.owner(), str(chat_id), **payload.model_dump())},
        )


@console_ns.route("/workbench/chats/<uuid:chat_id>/commands")
class Commands(WorkbenchResource):
    @console_ns.expect(console_ns.models[WorkbenchCommandPayload.__name__])
    @console_ns.response(200, "Idempotent command result", console_ns.models[WorkbenchCommandResponse.__name__])
    def post(self, chat_id):
        payload = WorkbenchCommandPayload.model_validate(console_ns.payload or {})
        return dump_response(
            WorkbenchCommandResponse,
            {
                "data": control.issue(*self.owner(), str(chat_id), **payload.model_dump(mode="json")),
            },
        )
