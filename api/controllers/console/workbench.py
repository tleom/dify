"""Authenticated workbench transport. Does not expose console editing credentials."""

import base64
import json
from collections.abc import Iterator
from typing import Annotated, Any, Literal
from urllib.parse import quote

from flask import Response, request, stream_with_context
from flask_restx import Resource
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from werkzeug.exceptions import Conflict, NotFound

from controllers.common.schema import query_params_from_model, register_response_schema_models, register_schema_models
from controllers.console import console_ns
from controllers.console.workbench_auth import workbench_login_required
from controllers.console.wraps import account_initialization_required, setup_required
from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from fields.base import ResponseModel
from fields.workbench_file_fields import WorkbenchFileLinksResponse, WorkbenchFileResponse
from libs.helper import dump_response
from libs.login import current_account_with_tenant
from services.workbench import scheduler, service
from services.workbench.event_log import owned_statement, read_state, stream_events
from services.workbench.mentions import ResourceMentions
from services.workbench.policy import Selection


class WorkbenchConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    selection: Selection


class WorkbenchIdentityUserResponse(ResponseModel):
    id: str
    name: str
    email: str


class WorkbenchIdentityWorkspaceResponse(ResponseModel):
    id: str
    name: str
    current: bool


class WorkbenchIdentityResponse(ResponseModel):
    user: WorkbenchIdentityUserResponse
    workspaces: list[WorkbenchIdentityWorkspaceResponse]
    workspaceId: str


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


class WorkbenchSteerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_run_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")


class WorkbenchRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    activity_protocol: Literal[0, 1] = 0
    queue_when_busy: bool = False
    continue_run_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    version: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=128)
    query: str = Field(max_length=100000)
    inputs: dict = Field(default_factory=dict)
    resource_mentions: ResourceMentions = Field(default_factory=ResourceMentions)
    parent_message_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    parent_run_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    files: list[WorkbenchSandboxFilePayload] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def require_message(self):
        if not self.query.strip() and not self.files and not self.continue_run_id:
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
    action: str | None = Field(default=None, min_length=1, max_length=100)


class WorkbenchFeedbackPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: Literal["like", "dislike"] | None


class WorkbenchInputInteractionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=200)


class WorkbenchHumanInputResponse(ResponseModel):
    request_id: str
    tool_call_id: str
    deadline_at: float | None
    interacted: bool
    server_now: float


class WorkbenchRecoveryResponse(ResponseModel):
    attempt: int
    limit: int
    pending: bool
    next_run_id: str | None = None


class WorkbenchRegeneratePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    activity_protocol: Literal[0, 1] = 0
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
    activity_protocol: Literal[1] = 1
    followup_protocol: Literal[1] = 1
    default_selection: Selection
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
    events_cursor: str | None = None
    activity_protocol: int = 0
    followup_protocol: int = 0
    is_continuation: bool = False
    user_paused: bool = False
    queue_order: int | None = None
    queue_selection: Selection | None = None
    queue_files: list[WorkbenchSandboxFilePayload] = Field(default_factory=list)
    steer_target_run_id: str | None = None
    steering_messages: list[dict[str, Any]] = Field(default_factory=list)
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
        "waiting_turn",
        "discarded",
        "steered",
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
    context_usage: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    human_input: WorkbenchHumanInputResponse | None = None
    recovery: WorkbenchRecoveryResponse | None = None


class WorkbenchRunEnvelopeResponse(ResponseModel):
    data: WorkbenchRunResponse


class WorkbenchFollowupsResponse(ResponseModel):
    runs: list[WorkbenchRunResponse]


class WorkbenchFollowupsEnvelopeResponse(ResponseModel):
    data: WorkbenchFollowupsResponse


class WorkbenchFollowupsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tracked: str = Field(default="", pattern=r"^(?:[0-9a-fA-F-]{36}(?:,[0-9a-fA-F-]{36}){0,3})?$")


class WorkbenchChatSummaryResponse(ResponseModel):
    created_at: int = Field(description="Conversation creation time in Unix seconds (UTC)")
    updated_at: int = Field(description="Conversation update time in Unix seconds (UTC)")
    file_directory: str | None = None
    id: str
    title: str
    version: int
    pinned: bool
    is_running: bool | None = Field(
        default=None, description="Whether this conversation has a queued or executing run; excludes waiting for input"
    )
    needs_input: bool | None = Field(default=None, description="Whether this conversation is waiting for user input")
    has_active_run: bool | None = Field(
        default=None,
        description="Whether run status still needs refreshing, including environment updates and input waits",
    )


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
    path: str = Field(default="conversations", min_length=1, max_length=1024)


class WorkbenchFileLinksQuery(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class WorkbenchEventsQuery(BaseModel):
    cursor: str = Field(default="0-0", pattern=r"^\d+-\d+$")


register_schema_models(
    console_ns,
    WorkbenchConfigPayload,
    WorkbenchChatPayload,
    WorkbenchRunPayload,
    WorkbenchSteerPayload,
    WorkbenchFollowupsQuery,
    WorkbenchFilePayload,
    WorkbenchFileLinksQuery,
    WorkbenchResumePayload,
    WorkbenchInputInteractionPayload,
    WorkbenchFeedbackPayload,
    WorkbenchRegeneratePayload,
)
register_response_schema_models(
    console_ns,
    WorkbenchIdentityResponse,
    WorkbenchCatalogEnvelopeResponse,
    WorkbenchRunEnvelopeResponse,
    WorkbenchFollowupsEnvelopeResponse,
    WorkbenchChatEnvelopeResponse,
    WorkbenchChatListResponse,
    WorkbenchFilesResponse,
    WorkbenchFileLinksResponse,
    WorkbenchUploadEnvelopeResponse,
    WorkbenchDeletedEnvelopeResponse,
    WorkbenchStopEnvelopeResponse,
    WorkbenchParameterRulesResponse,
    WorkbenchChatSummaryEnvelopeResponse,
    WorkbenchTranscriptEnvelopeResponse,
)


class WorkbenchResource(Resource):
    method_decorators = [account_initialization_required, workbench_login_required, setup_required]

    @staticmethod
    def owner() -> tuple[str, str]:
        account, tenant_id = current_account_with_tenant()
        service.authorize(tenant_id, account.id)
        return tenant_id, account.id


@console_ns.route("/workbench/identity")
class Identity(WorkbenchResource):
    @console_ns.response(200, "Current workbench identity", console_ns.models[WorkbenchIdentityResponse.__name__])
    def get(self):
        tenant_id, _ = self.owner()
        account, _ = current_account_with_tenant()
        tenant = account.current_tenant
        if tenant is None:
            raise NotFound("工作台租户已不可用")
        return dump_response(
            WorkbenchIdentityResponse,
            {
                "user": {"id": account.id, "name": account.name, "email": account.email},
                "workspaces": [{"id": tenant_id, "name": tenant.name, "current": True}],
                "workspaceId": tenant_id,
            },
        )


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


@console_ns.route("/workbench/chats/draft/audio", defaults={"chat_id": None})
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
            {"data": transcribe(*self.owner(), str(chat_id) if chat_id else None, request.files.get("file"))},
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


@console_ns.route("/workbench/files/links")
class FileLinks(WorkbenchResource):
    @console_ns.doc(params=query_params_from_model(WorkbenchFileLinksQuery))
    @console_ns.response(200, "Verified file-space links", console_ns.models[WorkbenchFileLinksResponse.__name__])
    def get(self):
        from services.workbench.file_links import lookup

        query = WorkbenchFileLinksQuery.model_validate(request.args.to_dict())
        return dump_response(WorkbenchFileLinksResponse, {"data": lookup(*self.owner(), query.path)})


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
        run = session.scalar(owned_statement(tenant_id, account_id, run_id))
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
        return dump_response(
            WorkbenchRunEnvelopeResponse,
            {
                "data": feedback(*self.owner(), str(run_id), payload.rating),
            },
        )


@console_ns.route("/workbench/runs/<uuid:run_id>/regenerate")
class Regenerate(WorkbenchResource):
    @console_ns.expect(console_ns.models[WorkbenchRegeneratePayload.__name__])
    @console_ns.response(
        202, "Dify Agent regeneration queued", console_ns.models[WorkbenchRunEnvelopeResponse.__name__]
    )
    def post(self, run_id):
        from services.workbench.message_actions import regenerate

        payload = WorkbenchRegeneratePayload.model_validate(console_ns.payload or {})
        return dump_response(
            WorkbenchRunEnvelopeResponse,
            {
                "data": regenerate(
                    *self.owner(),
                    str(run_id),
                    payload.version,
                    payload.request_key,
                    query=payload.query,
                    activity_protocol=payload.activity_protocol,
                ),
            },
        ), 202


@console_ns.route("/workbench/runs/<uuid:run_id>")
class Run(WorkbenchResource):
    @console_ns.response(
        200, "Task state and persisted events", console_ns.models[WorkbenchRunEnvelopeResponse.__name__]
    )
    def get(self, run_id):
        dto, _ = owned_run(*self.owner(), str(run_id))
        return dump_response(WorkbenchRunEnvelopeResponse, {"data": dto})


@console_ns.route("/workbench/chats/<uuid:chat_id>/followups")
class Followups(WorkbenchResource):
    @console_ns.doc(params=query_params_from_model(WorkbenchFollowupsQuery))
    @console_ns.response(
        200, "Live queue without event history", console_ns.models[WorkbenchFollowupsEnvelopeResponse.__name__]
    )
    def get(self, chat_id):
        from services.workbench.followups import snapshot

        query = WorkbenchFollowupsQuery.model_validate(request.args.to_dict(flat=True))
        return dump_response(
            WorkbenchFollowupsEnvelopeResponse,
            {"data": snapshot(*self.owner(), str(chat_id), query.tracked.split(",") if query.tracked else [])},
        )


@console_ns.route("/workbench/runs/<uuid:run_id>/queue")
class FollowupQueue(WorkbenchResource):
    @console_ns.response(
        200, "Queued message removed for deletion or editing", console_ns.models[WorkbenchRunEnvelopeResponse.__name__]
    )
    def delete(self, run_id):
        from services.workbench.followups import remove

        return dump_response(WorkbenchRunEnvelopeResponse, {"data": remove(*self.owner(), str(run_id))})


@console_ns.route("/workbench/runs/<uuid:run_id>/steer")
class Steer(WorkbenchResource):
    @console_ns.expect(console_ns.models[WorkbenchSteerPayload.__name__])
    @console_ns.response(
        200, "Queued message attached to the current task", console_ns.models[WorkbenchRunEnvelopeResponse.__name__]
    )
    def post(self, run_id):
        from services.workbench.followups import steer

        payload = WorkbenchSteerPayload.model_validate(console_ns.payload or {})
        return dump_response(
            WorkbenchRunEnvelopeResponse, {"data": steer(*self.owner(), str(run_id), payload.target_run_id)}
        )


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


@console_ns.route("/workbench/runs/<uuid:run_id>/input-interaction")
class InputInteraction(WorkbenchResource):
    @console_ns.expect(console_ns.models[WorkbenchInputInteractionPayload.__name__])
    @console_ns.response(
        200, "Human input countdown cancelled", console_ns.models[WorkbenchRunEnvelopeResponse.__name__]
    )
    def post(self, run_id):
        from services.workbench.recovery import interact

        payload = WorkbenchInputInteractionPayload.model_validate(console_ns.payload or {})
        return dump_response(
            WorkbenchRunEnvelopeResponse,
            {"data": interact(*self.owner(), str(run_id), payload.request_id)},
        )


@console_ns.route("/workbench/runs/<uuid:run_id>/input-timeout")
class InputTimeout(WorkbenchResource):
    @console_ns.expect(console_ns.models[WorkbenchInputInteractionPayload.__name__])
    @console_ns.response(
        200, "Current state after checking input deadline", console_ns.models[WorkbenchRunEnvelopeResponse.__name__]
    )
    def post(self, run_id):
        from services.workbench.recovery import expire_input

        payload = WorkbenchInputInteractionPayload.model_validate(console_ns.payload or {})
        owner = self.owner()
        expire_input(str(run_id), owner=owner, request_id=payload.request_id)
        dto, _ = owned_run(*owner, str(run_id))
        return dump_response(WorkbenchRunEnvelopeResponse, {"data": dto})


@console_ns.route("/workbench/runs/<uuid:run_id>/input-skip")
class InputSkip(WorkbenchResource):
    @console_ns.expect(console_ns.models[WorkbenchInputInteractionPayload.__name__])
    @console_ns.response(200, "Human question skipped", console_ns.models[WorkbenchRunEnvelopeResponse.__name__])
    def post(self, run_id):
        from services.workbench.recovery import expire_input

        payload = WorkbenchInputInteractionPayload.model_validate(console_ns.payload or {})
        owner = self.owner()
        expire_input(str(run_id), owner=owner, request_id=payload.request_id, manual=True)
        dto, _ = owned_run(*owner, str(run_id))
        return dump_response(WorkbenchRunEnvelopeResponse, {"data": dto})


@console_ns.route("/workbench/runs/<uuid:run_id>/stop")
class Stop(WorkbenchResource):
    @console_ns.response(
        200, "Targeted cancellation requested", console_ns.models[WorkbenchStopEnvelopeResponse.__name__]
    )
    def post(self, run_id):
        tenant_id, account_id = self.owner()
        from services.workbench.recovery import cancel_chain
        from tasks.workbench_tasks import force_stop

        for target in cancel_chain(tenant_id, account_id, str(run_id)):
            redis_client.setex(scheduler.PREFIX + "stop:" + target, 86400, "1")
            force_stop.delay(target, account_id)
        return dump_response(WorkbenchStopEnvelopeResponse, {"data": {"status": "cancelled"}})


@console_ns.route("/workbench/runs/<uuid:run_id>/events")
class Events(WorkbenchResource):
    @console_ns.doc(params=query_params_from_model(WorkbenchEventsQuery), produces=["text/event-stream"])
    @console_ns.response(200, "Resumable server-sent events; Last-Event-ID overrides cursor")
    def get(self, run_id):
        owner = self.owner()
        state = read_state(*owner, str(run_id))
        query = WorkbenchEventsQuery.model_validate(
            {"cursor": request.headers.get("Last-Event-ID") or request.args.get("cursor") or "0-0"}
        )
        cursor = query.cursor

        def generate() -> Iterator[str]:
            nonlocal cursor
            if state["activity_protocol"] == 1:
                for item in stream_events(*owner, str(run_id), after=int(cursor.split("-")[0])):
                    if item is None:
                        yield ": keepalive\n\n"
                    else:
                        identifier = f"id: {item['_id']}\n" if item.get("_id") else ""
                        yield identifier + "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
                return
            while True:
                items = redis_client.xread({scheduler.event_key(str(run_id)): cursor}, count=100, block=1000)
                for event_id, fields in scheduler.stream_entries(items):
                    cursor = event_id.decode() if isinstance(event_id, bytes) else event_id
                    data = fields.get(b"data", fields.get("data"))
                    text = data.decode() if isinstance(data, bytes) else data
                    if json.loads(text).get("event") == "workbench_end":
                        terminal_state = read_state(*owner, str(run_id))
                        if terminal_state["status"] == "stopping":
                            yield (
                                f"id: {cursor}\ndata: "
                                + json.dumps({"event": "workbench_status", "status": "stopping"})
                                + "\n\n"
                            )
                            continue
                    yield f"id: {cursor}\ndata: {text}\n\n"
                    if json.loads(text).get("event") == "workbench_end":
                        return
                terminal_state = read_state(*owner, str(run_id))
                if terminal_state["status"] not in (
                    "queued",
                    "running",
                    "environment_update",
                    "environment_installing",
                    "stopping",
                ):
                    if cursor == "0-0":
                        legacy_dto, _ = owned_run(*owner, str(run_id))
                        for event in legacy_dto["events"]:
                            yield "data: " + json.dumps(event) + "\n\n"
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "event": "workbench_end",
                                "status": terminal_state["status"],
                                "error": terminal_state["error"],
                                **({"recovery": terminal_state["recovery"]} if "recovery" in terminal_state else {}),
                            }
                        )
                        + "\n\n"
                    )
                    return
                yield ": keepalive\n\n"

        return Response(
            # Flask accepts Iterator[str]; its AnyStr overload is not resolved by pyrefly.
            stream_with_context(generate()),  # pyrefly: ignore[no-matching-overload]
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
