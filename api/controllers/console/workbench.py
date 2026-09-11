"""Authenticated workbench transport. Does not expose console editing credentials."""

import base64
import json
from typing import Annotated, Any, Literal
from urllib.parse import quote

from flask import Response, request, stream_with_context
from flask_restx import Resource
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select
from werkzeug.exceptions import Conflict, NotFound

from controllers.common.schema import query_params_from_model, register_response_schema_models, register_schema_models
from controllers.console import console_ns
from controllers.console.wraps import account_initialization_required, setup_required
from core.app.entities.app_invoke_entities import InvokeFrom
from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from fields.base import ResponseModel
from libs.helper import dump_response
from libs.login import current_account_with_tenant, login_required
from models.model import AppMode
from models.workbench import WorkbenchRun
from services.app_task_service import AppTaskService
from services.workbench import scheduler, service
from services.workbench.mentions import ResourceMentions
from services.workbench.policy import Selection


class WorkbenchConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    selection: Selection


class WorkbenchChatPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] | None = None
    pinned: bool | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.title is None and self.pinned is None:
            raise ValueError("请选择要更新的会话信息")
        return self


class WorkbenchSandboxFilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    version: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkbenchRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=128)
    query: str = Field(max_length=100000)
    inputs: dict = Field(default_factory=dict)
    resource_mentions: ResourceMentions = Field(default_factory=ResourceMentions)
    parent_message_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    files: list[WorkbenchSandboxFilePayload] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def require_message(self):
        if not self.query.strip() and not self.files:
            raise ValueError("请输入消息或添加附件")
        return self


class WorkbenchFilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    version: str | None
    data: str | None = Field(default=None, max_length=28000000)


class WorkbenchResumePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(default_factory=dict)
    action: str = Field(min_length=1, max_length=100)


class WorkbenchFeedbackPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: Literal["like", "dislike"] | None


class WorkbenchRegeneratePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=128)
    query: str | None = Field(default=None, max_length=100000)


class WorkbenchResourceResponse(ResponseModel):
    id: str
    name: str
    description: str | None = None
    group: str | None = None
    provider: str | None = None
    provider_name: str | None = None
    plugin_id: str | None = None
    tool_name: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class WorkbenchModelResponse(ResponseModel):
    id: str
    name: str
    provider: str


class WorkbenchCatalogResponse(ResponseModel):
    models: list[WorkbenchModelResponse]
    tools: list[WorkbenchResourceResponse]
    skills: list[WorkbenchResourceResponse]
    knowledge: list[WorkbenchResourceResponse]


class WorkbenchCatalogEnvelopeResponse(ResponseModel):
    data: WorkbenchCatalogResponse


class WorkbenchAttachmentResponse(ResponseModel):
    path: str
    name: str


class WorkbenchRunResponse(ResponseModel):
    id: str
    chat_id: str
    revision_id: str
    version: int
    query: str
    resource_mentions: ResourceMentions = Field(default_factory=ResourceMentions)
    mentioned_resources: list[dict[str, str]] = Field(default_factory=list)
    attachments: list[WorkbenchAttachmentResponse] = Field(default_factory=list)
    message_id: str | None = None
    feedback: Literal["like", "dislike"] | None = None
    regenerate_from: str | None = None
    edited_from: str | None = None
    parent_run_id: str | None = None
    parent_message_id: str | None = None
    status: Literal[
        "queued",
        "running",
        "waiting_input",
        "environment_update",
        "environment_installing",
        "completed",
        "failed",
        "cancelled",
        "stopping",
        "interrupted",
    ]
    error: str | None = None
    events: list[dict[str, Any]]
    pending: dict[str, Any] | None = None


class WorkbenchRunEnvelopeResponse(ResponseModel):
    data: WorkbenchRunResponse


class WorkbenchChatSummaryResponse(ResponseModel):
    id: str
    title: str
    version: int
    pinned: bool


class WorkbenchChatSummaryEnvelopeResponse(ResponseModel):
    data: WorkbenchChatSummaryResponse


class WorkbenchTranscriptResponse(ResponseModel):
    text: str


class WorkbenchTranscriptEnvelopeResponse(ResponseModel):
    data: WorkbenchTranscriptResponse


class WorkbenchChatResponse(WorkbenchChatSummaryResponse):
    template_snapshot_id: str
    selection: Selection
    runs: list[WorkbenchRunResponse]


class WorkbenchChatEnvelopeResponse(ResponseModel):
    data: WorkbenchChatResponse


class WorkbenchChatListResponse(ResponseModel):
    data: list[WorkbenchChatSummaryResponse]


class WorkbenchFileResponse(ResponseModel):
    name: str
    path: str
    kind: Literal["file", "directory", "blocked"]
    size: int
    modified: float
    version: str | None


class WorkbenchDirectoryResponse(ResponseModel):
    path: str
    entries: list[WorkbenchFileResponse]


class WorkbenchFilesResponse(ResponseModel):
    data: WorkbenchDirectoryResponse


class WorkbenchUploadResponse(ResponseModel):
    path: str
    version: str


class WorkbenchUploadEnvelopeResponse(ResponseModel):
    data: WorkbenchUploadResponse


class WorkbenchDeletedResponse(ResponseModel):
    deleted: bool


class WorkbenchDeletedEnvelopeResponse(ResponseModel):
    data: WorkbenchDeletedResponse


class WorkbenchStopResponse(ResponseModel):
    status: Literal["cancelled"]


class WorkbenchStopEnvelopeResponse(ResponseModel):
    data: WorkbenchStopResponse


class WorkbenchParameterRulesResponse(ResponseModel):
    data: dict[str, dict[str, Any]]


class WorkbenchModelQuery(BaseModel):
    model: str = Field(min_length=1, max_length=512)


class WorkbenchFileQuery(BaseModel):
    path: str = Field(default="shared", min_length=1, max_length=1024)


class WorkbenchEventsQuery(BaseModel):
    cursor: str = Field(default="0-0", pattern=r"^\d+-\d+$")


register_schema_models(
    console_ns,
    WorkbenchConfigPayload,
    WorkbenchChatPayload,
    WorkbenchRunPayload,
    WorkbenchFilePayload,
    WorkbenchResumePayload,
    WorkbenchFeedbackPayload,
    WorkbenchRegeneratePayload,
)
register_response_schema_models(
    console_ns,
    WorkbenchCatalogEnvelopeResponse,
    WorkbenchRunEnvelopeResponse,
    WorkbenchChatEnvelopeResponse,
    WorkbenchChatListResponse,
    WorkbenchFilesResponse,
    WorkbenchUploadEnvelopeResponse,
    WorkbenchDeletedEnvelopeResponse,
    WorkbenchStopEnvelopeResponse,
    WorkbenchParameterRulesResponse,
    WorkbenchChatSummaryEnvelopeResponse,
    WorkbenchTranscriptEnvelopeResponse,
)


class WorkbenchResource(Resource):
    method_decorators = [account_initialization_required, login_required, setup_required]

    @staticmethod
    def owner():
        account, tenant_id = current_account_with_tenant()
        service.authorize(tenant_id, account.id)
        return tenant_id, account.id


@console_ns.route("/workbench/catalog")
class Catalog(WorkbenchResource):
    @console_ns.response(
        200, "Authorized public resources", console_ns.models[WorkbenchCatalogEnvelopeResponse.__name__]
    )
    def get(self):
        return dump_response(WorkbenchCatalogEnvelopeResponse, {"data": service.catalog(*self.owner())})


@console_ns.route("/workbench/models/parameters")
class ModelParameters(WorkbenchResource):
    @console_ns.doc(params=query_params_from_model(WorkbenchModelQuery))
    @console_ns.response(200, "Model parameter rules", console_ns.models[WorkbenchParameterRulesResponse.__name__])
    def get(self):
        tenant_id, account_id = self.owner()
        service.template(tenant_id, account_id)
        models = service.models_and_rules(tenant_id)
        query = WorkbenchModelQuery.model_validate(request.args.to_dict(flat=True))
        model = models.get(query.model)
        if not model:
            raise NotFound()
        return dump_response(WorkbenchParameterRulesResponse, {"data": service.rules_for(tenant_id, model)})


@console_ns.route("/workbench/chats")
class Chats(WorkbenchResource):
    @console_ns.response(200, "Personal conversations", console_ns.models[WorkbenchChatListResponse.__name__])
    def get(self):
        return dump_response(WorkbenchChatListResponse, {"data": service.list_chats(*self.owner())})

    @console_ns.response(201, "Conversation created", console_ns.models[WorkbenchChatEnvelopeResponse.__name__])
    def post(self):
        return dump_response(WorkbenchChatEnvelopeResponse, {"data": service.create_chat(*self.owner())}), 201


@console_ns.route("/workbench/chats/<uuid:chat_id>")
class Chat(WorkbenchResource):
    @console_ns.response(200, "Conversation and revisions", console_ns.models[WorkbenchChatEnvelopeResponse.__name__])
    def get(self, chat_id):
        return dump_response(WorkbenchChatEnvelopeResponse, {"data": service.read_chat(*self.owner(), str(chat_id))})

    @console_ns.expect(console_ns.models[WorkbenchChatPayload.__name__])
    @console_ns.response(
        200, "Conversation renamed or pinned", console_ns.models[WorkbenchChatSummaryEnvelopeResponse.__name__]
    )
    def patch(self, chat_id):
        payload = WorkbenchChatPayload.model_validate(console_ns.payload or {})
        return dump_response(
            WorkbenchChatSummaryEnvelopeResponse,
            {"data": service.update_chat(*self.owner(), str(chat_id), **payload.model_dump(exclude_none=True))},
        )

    @console_ns.response(204, "Conversation deleted")
    def delete(self, chat_id):
        service.delete_chat(*self.owner(), str(chat_id))
        return "", 204


@console_ns.route("/workbench/chats/<uuid:chat_id>/audio")
class ChatAudio(WorkbenchResource):
    @console_ns.doc(
        consumes=["multipart/form-data"], params={"file": {"in": "formData", "type": "file", "required": True}}
    )
    @console_ns.response(200, "Voice input transcript", console_ns.models[WorkbenchTranscriptEnvelopeResponse.__name__])
    @console_ns.response(400, "Missing or unsupported audio")
    @console_ns.response(413, "Audio file too large")
    def post(self, chat_id):
        from services.workbench.audio import transcribe

        return dump_response(
            WorkbenchTranscriptEnvelopeResponse,
            {"data": transcribe(*self.owner(), str(chat_id), request.files.get("file"))},
        )


@console_ns.route("/workbench/chats/<uuid:chat_id>/config")
class Configuration(WorkbenchResource):
    @console_ns.expect(console_ns.models[WorkbenchConfigPayload.__name__])
    @console_ns.response(
        200, "New immutable configuration revision", console_ns.models[WorkbenchChatEnvelopeResponse.__name__]
    )
    def put(self, chat_id):
        payload = WorkbenchConfigPayload.model_validate(console_ns.payload or {})
        return dump_response(
            WorkbenchChatEnvelopeResponse,
            {"data": service.update_config(*self.owner(), str(chat_id), payload.version, payload.selection)},
        )


@console_ns.route("/workbench/chats/<uuid:chat_id>/runs")
class Runs(WorkbenchResource):
    @console_ns.expect(console_ns.models[WorkbenchRunPayload.__name__])
    @console_ns.response(
        202, "Task queued with frozen configuration", console_ns.models[WorkbenchRunEnvelopeResponse.__name__]
    )
    def post(self, chat_id):
        payload = WorkbenchRunPayload.model_validate(console_ns.payload or {})
        return dump_response(
            WorkbenchRunEnvelopeResponse,
            {
                "data": service.enqueue(
                    *self.owner(),
                    str(chat_id),
                    payload.version,
                    payload.request_key,
                    payload.model_dump(exclude={"version", "request_key"}, exclude_unset=True),
                )
            },
        ), 202


@console_ns.route("/workbench/files")
class Files(WorkbenchResource):
    @console_ns.doc(params=query_params_from_model(WorkbenchFileQuery))
    @console_ns.response(200, "Personal directory", console_ns.models[WorkbenchFilesResponse.__name__])
    def get(self):
        from services.workbench.files import operate

        query = WorkbenchFileQuery.model_validate(request.args.to_dict(flat=True))
        return dump_response(WorkbenchFilesResponse, {"data": operate(*self.owner(), "list", query.path)})

    @console_ns.expect(console_ns.models[WorkbenchFilePayload.__name__])
    @console_ns.response(200, "Atomic upload result", console_ns.models[WorkbenchUploadEnvelopeResponse.__name__])
    def post(self):
        from services.workbench.files import operate

        payload = WorkbenchFilePayload.model_validate(console_ns.payload or {})
        if payload.data is None:
            raise Conflict("请选择上传文件")
        return dump_response(
            WorkbenchUploadEnvelopeResponse,
            {"data": operate(*self.owner(), "upload", payload.path, version=payload.version, data=payload.data)},
        )

    @console_ns.expect(console_ns.models[WorkbenchFilePayload.__name__])
    @console_ns.response(200, "Version checked deletion", console_ns.models[WorkbenchDeletedEnvelopeResponse.__name__])
    def delete(self):
        from services.workbench.files import operate

        payload = WorkbenchFilePayload.model_validate(console_ns.payload or {})
        return dump_response(
            WorkbenchDeletedEnvelopeResponse,
            {"data": operate(*self.owner(), "delete", payload.path, version=payload.version)},
        )


@console_ns.route("/workbench/files/download")
class Download(WorkbenchResource):
    @console_ns.doc(params=query_params_from_model(WorkbenchFileQuery), produces=["application/octet-stream"])
    @console_ns.response(200, "File bytes with ETag and attachment filename")
    def get(self):
        from services.workbench.files import operate

        query = WorkbenchFileQuery.model_validate(request.args.to_dict(flat=True))
        result = operate(*self.owner(), "get", query.path)
        return Response(
            base64.b64decode(result["data"]),
            mimetype="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(result["name"], safe=""),
                "ETag": '"' + result["version"] + '"',
                "Cache-Control": "no-store",
            },
        )


def owned_run(tenant_id, account_id, run_id):
    from services.workbench.message_actions import with_feedback

    with session_factory.create_session() as session:
        run = session.scalar(
            select(WorkbenchRun).where(
                WorkbenchRun.id == run_id, WorkbenchRun.tenant_id == tenant_id, WorkbenchRun.account_id == account_id
            )
        )
        if run is None:
            raise NotFound()
        return with_feedback(session, [run], [service.run_dto(run)])[0], run.task_id


@console_ns.route("/workbench/runs/<uuid:run_id>/feedbacks")
class Feedback(WorkbenchResource):
    @console_ns.expect(console_ns.models[WorkbenchFeedbackPayload.__name__])
    @console_ns.response(200, "Dify message feedback", console_ns.models[WorkbenchRunEnvelopeResponse.__name__])
    def post(self, run_id):
        from services.workbench.message_actions import feedback

        payload = WorkbenchFeedbackPayload.model_validate(console_ns.payload or {})
        return dump_response(WorkbenchRunEnvelopeResponse, {
            "data": feedback(*self.owner(), str(run_id), payload.rating),
        })


@console_ns.route("/workbench/runs/<uuid:run_id>/regenerate")
class Regenerate(WorkbenchResource):
    @console_ns.expect(console_ns.models[WorkbenchRegeneratePayload.__name__])
    @console_ns.response(
        202, "Dify Agent regeneration queued", console_ns.models[WorkbenchRunEnvelopeResponse.__name__]
    )
    def post(self, run_id):
        from services.workbench.message_actions import regenerate

        payload = WorkbenchRegeneratePayload.model_validate(console_ns.payload or {})
        return dump_response(WorkbenchRunEnvelopeResponse, {
            "data": regenerate(*self.owner(), str(run_id), payload.version, payload.request_key, query=payload.query),
        }), 202


@console_ns.route("/workbench/runs/<uuid:run_id>")
class Run(WorkbenchResource):
    @console_ns.response(
        200, "Task state and persisted events", console_ns.models[WorkbenchRunEnvelopeResponse.__name__]
    )
    def get(self, run_id):
        dto, _ = owned_run(*self.owner(), str(run_id))
        return dump_response(WorkbenchRunEnvelopeResponse, {"data": dto})


@console_ns.route("/workbench/runs/<uuid:run_id>/resume")
class Resume(WorkbenchResource):
    @console_ns.response(
        200, "Task resumed with original configuration", console_ns.models[WorkbenchRunEnvelopeResponse.__name__]
    )
    @console_ns.expect(console_ns.models[WorkbenchResumePayload.__name__])
    def post(self, run_id):
        payload = WorkbenchResumePayload.model_validate(console_ns.payload or {})
        return dump_response(
            WorkbenchRunEnvelopeResponse,
            {"data": service.resume(*self.owner(), str(run_id), payload.values, payload.action)},
        )


@console_ns.route("/workbench/runs/<uuid:run_id>/stop")
class Stop(WorkbenchResource):
    @console_ns.response(
        200, "Targeted cancellation requested", console_ns.models[WorkbenchStopEnvelopeResponse.__name__]
    )
    def post(self, run_id):
        tenant_id, account_id = self.owner()
        dto, task_id = owned_run(tenant_id, account_id, str(run_id))
        redis_client.setex(scheduler.PREFIX + "stop:" + str(run_id), 86400, "1")
        with session_factory.get_session_maker().begin() as session:
            run = session.get(WorkbenchRun, str(run_id))
            if run.status in ("queued", "running", "waiting_input", "environment_update", "environment_installing"):
                run.status = "cancelled"
        if task_id:
            AppTaskService.stop_task(task_id, InvokeFrom.EXPLORE, account_id, AppMode.AGENT)
        from tasks.workbench_tasks import force_stop

        force_stop.delay(str(run_id), account_id)
        return dump_response(WorkbenchStopEnvelopeResponse, {"data": {"status": "cancelled"}})


@console_ns.route("/workbench/runs/<uuid:run_id>/events")
class Events(WorkbenchResource):
    @console_ns.doc(params=query_params_from_model(WorkbenchEventsQuery), produces=["text/event-stream"])
    @console_ns.response(200, "Resumable server-sent events; Last-Event-ID overrides cursor")
    def get(self, run_id):
        owner = self.owner()
        dto, _ = owned_run(*owner, str(run_id))
        query = WorkbenchEventsQuery.model_validate(
            {"cursor": request.headers.get("Last-Event-ID") or request.args.get("cursor") or "0-0"}
        )
        cursor = query.cursor

        @stream_with_context
        def generate():
            nonlocal cursor
            while True:
                items = redis_client.xread({scheduler.event_key(str(run_id)): cursor}, count=100, block=1000)
                for event_id, fields in scheduler.stream_entries(items):
                    cursor = event_id.decode() if isinstance(event_id, bytes) else event_id
                    data = fields.get(b"data", fields.get("data"))
                    text = data.decode() if isinstance(data, bytes) else data
                    if json.loads(text).get("event") == "workbench_end":
                        state, _ = owned_run(*owner, str(run_id))
                        if state["status"] == "stopping":
                            yield f"id: {cursor}\ndata: " + json.dumps(
                                {"event": "workbench_status", "status": "stopping"}
                            ) + "\n\n"
                            continue
                    yield f"id: {cursor}\ndata: {text}\n\n"
                    if json.loads(text).get("event") == "workbench_end":
                        return
                state, _ = owned_run(*owner, str(run_id))
                if state["status"] not in (
                    "queued", "running", "environment_update", "environment_installing", "stopping"
                ):
                    if cursor == "0-0":
                        for event in state["events"]:
                            yield "data: " + json.dumps(event) + "\n\n"
                    yield (
                        "data: "
                        + json.dumps({"event": "workbench_end", "status": state["status"], "error": state["error"]})
                        + "\n\n"
                    )
                    return
                yield ": keepalive\n\n"

        return Response(
            generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        )
