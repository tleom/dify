"""Account-scoped configuration transactions for the Agent workbench."""

from __future__ import annotations

import json
from typing import Any, TypedDict
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql.elements import ColumnElement
from werkzeug.exceptions import Conflict, Forbidden, NotFound

from configs import dify_config
from core.db.session_factory import session_factory
from libs.datetime_utils import naive_utc_now, to_utc_timestamp
from models import Account, TenantAccountJoin
from models.agent import Agent, AgentConfigSnapshot
from models.agent_config_entities import AgentSoulConfig
from models.provider_ids import ModelProviderID
from models.workbench import WorkbenchChat, WorkbenchRevision, WorkbenchRun
from services.agent.roster_service import AgentRosterService
from services.model_provider_service import ModelProviderService
from services.workbench.authorization import require_agent_run
from services.workbench.catalog_labels import enrich_skill_labels, enrich_tool_labels
from services.workbench.policy import (
    Selection,
    compile_selection,
    prune_unavailable_selection,
    public_resources,
    template_resources,
)


class WorkbenchTemplate(TypedDict):
    agent_id: str
    app_id: str
    snapshot_id: str
    soul: dict[str, Any]


EXECUTING_STATUSES = ("queued", "running", "environment_installing", "stopping")
ACTIVE_STATUSES = (*EXECUTING_STATUSES, "environment_update", "waiting_input")


def _chat_has_run(tenant_id, account_id, statuses):
    status_filter: ColumnElement[bool] = WorkbenchRun.status.in_(statuses)
    if statuses == ACTIVE_STATUSES:
        from services.workbench.recovery import pending_condition

        status_filter = or_(status_filter, pending_condition())
    return (
        select(WorkbenchRun.id)
        .where(
            WorkbenchRun.chat_id == WorkbenchChat.id,
            WorkbenchRun.tenant_id == tenant_id,
            WorkbenchRun.account_id == account_id,
            status_filter,
        )
        .exists()
    )


def authorize(tenant_id: str, account_id: str):
    if not dify_config.WORKBENCH_ENABLED:
        raise NotFound()
    allowed = dify_config.WORKBENCH_ALLOWED_ACCOUNTS.get(tenant_id)
    if allowed and account_id not in allowed:
        raise Forbidden("工作台尚未向此账号开放")
    with session_factory.create_session() as session:
        membership = session.scalar(
            select(TenantAccountJoin.id).where(
                TenantAccountJoin.tenant_id == tenant_id, TenantAccountJoin.account_id == account_id
            )
        )
        account = session.get(Account, account_id)
        if membership is None or account is None or account.status != "active":
            raise Forbidden()


def _authorized_template(tenant_id: str, account_id: str) -> WorkbenchTemplate:
    """Check the published Agent and caller without discovering current resources."""
    authorize(tenant_id, account_id)
    agent_id = dify_config.WORKBENCH_AGENT_TEMPLATES.get(tenant_id)
    with session_factory.create_session() as session:
        agent = (
            session.scalar(
                select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id, Agent.status == "active")
            )
            if agent_id
            else None
        )
        if agent is None or not agent.active_config_snapshot_id or not agent.active_config_is_published:
            raise Conflict("管理员尚未配置已发布的通用 Agent")
        snapshot = session.get(AgentConfigSnapshot, agent.active_config_snapshot_id)
        if snapshot is None:
            raise Conflict("通用 Agent 发布版本不可用")
        app = AgentRosterService(session).get_agent_runtime_app_model(tenant_id=tenant_id, agent_id=agent.id)
        from services.workbench.gxzs_identity import can_run_template

        if not can_run_template(session, tenant_id, account_id, agent.id):
            require_agent_run(tenant_id, account_id, agent.id)
        base: WorkbenchTemplate = {
            "agent_id": agent.id,
            "app_id": app.id,
            "snapshot_id": snapshot.id,
            "soul": snapshot.config_snapshot_dict,
        }
    return base


def template(tenant_id: str, account_id: str) -> WorkbenchTemplate:
    base = _authorized_template(tenant_id, account_id)
    from core.workflow.nodes.agent_v2.dify_tools_builder import WorkflowAgentDifyToolsBuilder

    soul = AgentSoulConfig.model_validate(base["soul"])
    expanded = WorkflowAgentDifyToolsBuilder().expand_provider_entries(
        tenant_id=tenant_id, enabled_tools=[tool for tool in soul.tools.dify_tools if tool.enabled]
    )
    base["soul"]["tools"]["dify_tools"] = [tool.model_dump(mode="json") for tool in expanded]
    # Freeze published workspace Skills as concrete archive references as well.
    from services.skill_management_service import SkillManagementService

    names = {item["name"] for item in base["soul"].get("config_skills", [])}
    for skill in SkillManagementService().list_runtime_agent_skills(tenant_id=tenant_id, agent_id=base["agent_id"]):
        if skill["name"] not in names:
            base["soul"].setdefault("config_skills", []).append(
                {key: value for key, value in skill.items() if key != "id"}
            )
            names.add(skill["name"])
    from services.workbench.knowledge import available_sets

    base["soul"]["knowledge"] = {"sets": available_sets(tenant_id, account_id)}
    return base


def models_and_rules(tenant_id: str):
    service = ModelProviderService()
    models = {}
    for provider in service.get_models_by_model_type(tenant_id, "llm"):
        provider_id = ModelProviderID(provider.provider)
        for model in provider.models:
            # Every workbench Agent uses runtime tools, including its personal Shell.
            # A model without function calling silently turns executable tasks into prose.
            if model.status != "active" or not {"tool-call", "multi-tool-call"}.intersection(model.features or []):
                continue
            key = f"{provider.provider}::{model.model}"
            models[key] = {
                "plugin_id": provider_id.plugin_id,
                "model_provider": provider.provider,
                "model": model.model,
                "model_settings": {},
            }
    return models


def rules_for(tenant_id: str, model: dict):
    return {
        rule.name: rule.model_dump(mode="json")
        for rule in ModelProviderService().get_model_parameter_rules(tenant_id, model["model_provider"], model["model"])
    }


def catalog(tenant_id: str, account_id: str):
    base = template(tenant_id, account_id)
    models = models_and_rules(tenant_id)
    resources = public_resources(base["soul"], dify_config.WORKBENCH_TOOL_PARAMETERS.get(tenant_id))
    enrich_tool_labels(tenant_id, resources["tools"])
    enrich_skill_labels(tenant_id, base["agent_id"], resources["skills"])
    return {
        **resources,
        "activity_protocol": 1,
        "followup_protocol": 1,
        "default_selection": default_selection(tenant_id, account_id, base).model_dump(mode="json"),
        "models": [
            {"id": key, "name": value["model"], "provider": value["model_provider"]} for key, value in models.items()
        ],
    }


def compile_config(tenant_id: str, base: WorkbenchTemplate, selection: Selection):
    models = models_and_rules(tenant_id)
    rules = rules_for(tenant_id, models[selection.model]) if selection.model in models else {}
    effective = compile_selection(
        base["soul"], selection, models, rules, dify_config.WORKBENCH_TOOL_PARAMETERS.get(tenant_id)
    )
    # Run Dify's own runtime shape validation before persisting a revision.
    return AgentSoulConfig.model_validate(effective).model_dump(mode="json")


def _chat(session, tenant_id, account_id, chat_id, lock=False):
    query = select(WorkbenchChat).where(
        WorkbenchChat.id == chat_id,
        WorkbenchChat.tenant_id == tenant_id,
        WorkbenchChat.account_id == account_id,
        WorkbenchChat.deleted == 0,
    )
    if lock:
        query = query.with_for_update()
    chat = session.scalar(query)
    if chat is None:
        raise NotFound()
    return chat


def read_chat(tenant_id: str, account_id: str, chat_id: str):
    from services.workbench.branches import annotate
    from services.workbench.directories import chat_directory
    from services.workbench.message_actions import with_feedback
    from services.workbench.recovery import recovery_dto

    authorize(tenant_id, account_id)
    with session_factory.create_session() as session:
        chat = _chat(session, tenant_id, account_id, chat_id)
        revision = session.scalar(
            select(WorkbenchRevision).where(
                WorkbenchRevision.chat_id == chat.id, WorkbenchRevision.version == chat.version
            )
        )
        if revision is None:
            raise Conflict("会话配置已不可用，请新建会话")
        runs = list(
            session.scalars(
                select(WorkbenchRun)
                .where(
                    WorkbenchRun.chat_id == chat.id,
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                    WorkbenchRun.status.not_in(("discarded", "steered")),
                )
                .order_by(WorkbenchRun.created_at, WorkbenchRun.id)
            )
        )
        return {
            "id": chat.id,
            "title": chat.title,
            "file_directory": chat_directory(session, chat),
            "created_at": to_utc_timestamp(chat.created_at),
            "updated_at": to_utc_timestamp(chat.updated_at),
            "pinned": chat.pinned,
            "version": chat.version,
            "is_running": any(run.status in EXECUTING_STATUSES for run in runs),
            "needs_input": any(run.status == "waiting_input" for run in runs),
            "has_active_run": any(
                run.status in ACTIVE_STATUSES or (recovery_dto(json.loads(run.payload)) or {}).get("pending")
                for run in runs
            ),
            "template_snapshot_id": revision.template_snapshot_id or chat.base_snapshot_id,
            "selection": json.loads(revision.selection),
            "runs": annotate(runs, with_feedback(session, runs, [run_dto(run) for run in runs])),
        }


def run_dto(run, *, include_events=True):
    from services.workbench.context_status import merge_context_events
    from services.workbench.knowledge_events import run_knowledge_events
    from services.workbench.message_actions import message_ids

    payload = json.loads(run.payload)
    from services.workbench.recovery import input_dto, recovery_dto

    ids = message_ids(run) if include_events else payload.get("message_ids", [])
    from services.workbench.event_log import history_snapshot, uses_journal

    journal = uses_journal(run, payload)
    history, cursor = history_snapshot(run) if journal and include_events else (None, None)
    return {
        "id": run.id,
        "chat_id": run.chat_id,
        "revision_id": run.revision_id,
        "version": payload.get("version", 1),
        "pending": payload.get("pending"),
        "human_input": input_dto(payload),
        "recovery": recovery_dto(payload),
        "status": run.status,
        "error": run.error,
        "events_cursor": cursor,
        "events": []
        if not include_events
        else history
        if history is not None
        else [*run_knowledge_events(run, payload), *merge_context_events(run, payload)],
        "activity_protocol": 1 if journal else 0,
        "followup_protocol": int(payload.get("followup_protocol") == 1),
        "is_continuation": bool(payload.get("is_continuation")),
        "user_paused": bool(payload.get("user_paused")) and run.status == "cancelled",
        "queue_order": payload.get("queue_order"),
        "queue_selection": payload.get("queue_selection"),
        "queue_files": payload.get("queue_files", []),
        "steer_target_run_id": payload.get("steer_target_run_id"),
        "steering_messages": payload.get("steering_messages", []),
        "context_usage": payload.get("context_usage"),
        "query": payload.get("query", ""),
        "resource_mentions": payload.get("resource_mentions", {}),
        "mentioned_resources": payload.get("mentioned_resources", []),
        "message_id": ids[-1] if ids else None,
        "regenerate_from": payload.get("regenerate_from"),
        "parent_run_id": payload.get("branch_parent_run_id"),
        "parent_message_id": payload.get("parent_message_id"),
        "edited_from": payload.get("edited_from"),
        "attachments": [
            {"path": path.removeprefix("/workspace/"), "name": path.rsplit("/", 1)[-1]}
            for path in payload.get("sandbox_paths", [])
            if isinstance(path, str) and path.startswith("/workspace/") and not path.endswith("/")
        ],
    }


def list_chats(tenant_id, account_id):
    from services.workbench.directories import chat_directory

    authorize(tenant_id, account_id)
    with session_factory.create_session() as session:
        return [
            {
                "id": c.id,
                "title": c.title,
                "created_at": to_utc_timestamp(c.created_at),
                "updated_at": to_utc_timestamp(c.updated_at),
                "version": c.version,
                "pinned": c.pinned,
                "is_running": is_running,
                "needs_input": needs_input,
                "has_active_run": has_active_run,
                "file_directory": chat_directory(session, c),
            }
            for c, is_running, needs_input, has_active_run in session.execute(
                select(
                    WorkbenchChat,
                    _chat_has_run(tenant_id, account_id, EXECUTING_STATUSES),
                    _chat_has_run(tenant_id, account_id, ("waiting_input",)),
                    _chat_has_run(tenant_id, account_id, ACTIVE_STATUSES),
                )
                .where(
                    WorkbenchChat.tenant_id == tenant_id,
                    WorkbenchChat.account_id == account_id,
                    WorkbenchChat.deleted == 0,
                )
                .order_by(WorkbenchChat.updated_at.desc(), WorkbenchChat.created_at.desc(), WorkbenchChat.id.desc())
                .limit(200)
            )
        ]


def update_chat(tenant_id, account_id, chat_id, *, title=None, pinned=None):
    from services.workbench.directories import chat_directory

    authorize(tenant_id, account_id)
    with session_factory.get_session_maker().begin() as session:
        chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
        if title is not None:
            chat.title = title
        if pinned is not None:
            chat.pinned = pinned
            if title is None:
                # Bookmark changes do not count as conversation activity.
                flag_modified(chat, "updated_at")
        session.flush()
        return {
            "id": chat.id,
            "title": chat.title,
            "created_at": to_utc_timestamp(chat.created_at),
            "updated_at": to_utc_timestamp(chat.updated_at),
            "version": chat.version,
            "pinned": chat.pinned,
            "file_directory": chat_directory(session, chat),
            "is_running": session.scalar(
                select(_chat_has_run(tenant_id, account_id, EXECUTING_STATUSES)).where(WorkbenchChat.id == chat.id)
            ),
            "needs_input": session.scalar(
                select(_chat_has_run(tenant_id, account_id, ("waiting_input",))).where(WorkbenchChat.id == chat.id)
            ),
            "has_active_run": session.scalar(
                select(_chat_has_run(tenant_id, account_id, ACTIVE_STATUSES)).where(WorkbenchChat.id == chat.id)
            ),
        }


def default_selection(tenant_id, account_id, base):
    """Resolve personal draft defaults without creating a chat or revision."""
    with session_factory.create_session() as session:
        recent = session.scalar(
            select(WorkbenchRevision)
            .where(WorkbenchRevision.tenant_id == tenant_id, WorkbenchRevision.account_id == account_id)
            .order_by(WorkbenchRevision.created_at.desc())
            .limit(1)
        )
        if recent:
            selection = Selection.model_validate_json(recent.selection)
        else:
            resources = template_resources(base["soul"])
            model = base["soul"].get("model") or {}
            selection = Selection(
                model=f"{model.get('model_provider')}::{model.get('model')}",
                tools=list(resources["tools"]),
                skills=list(resources["skills"]),
                knowledge=list(resources["knowledge"]),
            )
    from services.workbench.mentions import default_capabilities

    selection = prune_unavailable_selection(base["soul"], selection)
    return default_capabilities(base["soul"], selection, new_chat=True)


def create_chat(tenant_id, account_id):
    base = template(tenant_id, account_id)
    selection = default_selection(tenant_id, account_id, base)
    chat_id, revision_id = str(uuid4()), str(uuid4())
    with session_factory.get_session_maker().begin() as session:
        session.add(
            WorkbenchChat(
                id=chat_id,
                tenant_id=tenant_id,
                account_id=account_id,
                agent_id=base["agent_id"],
                app_id=base["app_id"],
                base_snapshot_id=base["snapshot_id"],
                version=1,
            )
        )
        session.add(
            WorkbenchRevision(
                id=revision_id,
                tenant_id=tenant_id,
                account_id=account_id,
                chat_id=chat_id,
                version=1,
                template_snapshot_id=base["snapshot_id"],
                selection=selection.model_dump_json(),
                effective_soul="{}",
            )
        )
    return read_chat(tenant_id, account_id, chat_id)


def update_config(tenant_id, account_id, chat_id, version, selection):
    base = template(tenant_id, account_id)
    effective = compile_config(tenant_id, base, selection)
    with session_factory.get_session_maker().begin() as session:
        chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
        if chat.version != version:
            raise Conflict("配置已在其他页面修改，请刷新后重试")
        # Keep the base published generation fixed for existing workspace bindings.
        if chat.agent_id != base["agent_id"]:
            raise Conflict("管理员已切换通用 Agent，请新建会话")
        chat.version += 1
        # Configuration selection is not new conversation activity.
        flag_modified(chat, "updated_at")
        session.add(
            WorkbenchRevision(
                id=str(uuid4()),
                tenant_id=tenant_id,
                account_id=account_id,
                chat_id=chat.id,
                version=chat.version,
                template_snapshot_id=base["snapshot_id"],
                selection=selection.model_dump_json(),
                effective_soul=json.dumps(effective),
            )
        )
    return read_chat(tenant_id, account_id, chat_id)


def enqueue(tenant_id, account_id, chat_id, version, request_key, payload: dict[str, Any]):
    from services.workbench.followups import WAITING, pending_runs, queued_parent
    from services.workbench.recovery import pending_condition

    # A committed send remains retryable even after its original configuration
    # or attachment changes. Recheck under the chat lock below for concurrent sends.
    authorize(tenant_id, account_id)
    with session_factory.create_session() as session:
        _chat(session, tenant_id, account_id, chat_id)
        existing = session.scalar(
            select(WorkbenchRun).where(
                WorkbenchRun.chat_id == chat_id,
                WorkbenchRun.tenant_id == tenant_id,
                WorkbenchRun.account_id == account_id,
                WorkbenchRun.request_key == request_key,
            )
        )
        if existing:
            return run_dto(existing)

    silent_continue = bool(
        payload.get("continue_run_id") and not payload.get("query", "").strip() and not payload.get("files")
    )
    prepared = None
    if silent_continue:
        # Continue carries no new configuration or input resources. Keep its
        # authorization check, but do not discover providers for another draft.
        base = _authorized_template(tenant_id, account_id)
        payload = {
            "query": "继续",
            "continue_run_id": payload["continue_run_id"],
            "queue_when_busy": payload.get("queue_when_busy", False),
            "activity_protocol": payload.get("activity_protocol", 0),
        }
    else:
        base = template(tenant_id, account_id)
        prepared = _prepare_message(tenant_id, account_id, chat_id, base, payload)
        payload = prepared[0]
    with session_factory.get_session_maker().begin() as session:
        chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
        existing = session.scalar(
            select(WorkbenchRun).where(WorkbenchRun.chat_id == chat.id, WorkbenchRun.request_key == request_key)
        )
        if existing:
            return run_dto(existing)
        if prepared is not None and (chat.version != version or prepared[1]["version"] != version):
            raise Conflict("配置版本已改变，请刷新后发送")
        if chat.agent_id != base["agent_id"]:
            raise Conflict("管理员已切换通用 Agent，请新建会话")
        active = session.scalar(
            select(WorkbenchRun)
            .where(
                WorkbenchRun.chat_id == chat.id,
                WorkbenchRun.tenant_id == tenant_id,
                WorkbenchRun.account_id == account_id,
                WorkbenchRun.status.in_(ACTIVE_STATUSES),
            )
            .with_for_update()
        )
        if active is None and payload.get("queue_when_busy"):
            # Automatic recovery is still the current logical task. New input
            # joins its queue while the failed executor is being fenced.
            active = session.scalar(
                select(WorkbenchRun)
                .where(
                    WorkbenchRun.chat_id == chat.id,
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                    pending_condition(),
                )
                .order_by(WorkbenchRun.created_at.desc(), WorkbenchRun.id.desc())
                .with_for_update()
            )
        waiting = pending_runs(session, chat)
        defer = bool(active or waiting)
        continue_id = payload.get("continue_run_id")
        paused_parent = None
        if continue_id:
            paused_parent = session.scalar(
                select(WorkbenchRun)
                .where(
                    WorkbenchRun.id == continue_id,
                    WorkbenchRun.chat_id == chat.id,
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                )
                .with_for_update()
            )
            if paused_parent is None:
                raise NotFound()
            if active is not None or paused_parent.status != "cancelled":
                raise Conflict("原任务已继续或状态改变，输入已保留，请刷新后发送")
            if json.loads(paused_parent.payload).get("continued_by"):
                raise Conflict("原任务已继续，输入已保留，请刷新后发送")
            if waiting and json.loads(waiting[0].payload).get("branch_parent_run_id") != continue_id:
                raise Conflict("排队消息属于另一个任务，请返回该任务后继续")
            defer = False
        if defer and not continue_id and not payload.get("queue_when_busy"):
            raise Conflict("此会话已有任务，请等待完成或停止")
        if silent_continue:
            if paused_parent is None:
                raise NotFound()
            revision_filter = WorkbenchRevision.id == paused_parent.revision_id
        else:
            revision_filter = WorkbenchRevision.version == version
        revision = session.scalar(
            select(WorkbenchRevision).where(
                WorkbenchRevision.chat_id == chat.id,
                WorkbenchRevision.tenant_id == tenant_id,
                WorkbenchRevision.account_id == account_id,
                revision_filter,
            )
        )
        if revision is None:
            raise Conflict("会话配置已不可用，请新建会话")
        previous: dict[str, Any] = {}
        if prepared is None:
            if paused_parent is None:
                raise NotFound()
            previous = json.loads(paused_parent.payload)
            effective = previous.get("effective_soul")
            if effective is None:
                raise Conflict("原任务的配置已不可用，请重新生成")
            selected = Selection.model_validate(previous.get("queue_selection") or json.loads(revision.selection))
            version = revision.version
        else:
            _, _, selected, effective = prepared
        # The task's effective configuration is frozen independently of future template edits.
        from services.workbench.branches import resolve_parent

        parent = (
            resolve_parent(session, chat, {"parent_run_id": continue_id})
            if continue_id
            else queued_parent(session, chat, active, payload)
            if defer
            else resolve_parent(session, chat, payload)
        )
        payload = {
            **payload,
            **parent,
            "effective_soul": effective,
            "version": version,
            "attempt": 0,
            "recovery": {"attempt": 0},
            "followup_protocol": int(payload.get("followup_protocol") == 1 or bool(payload.get("queue_when_busy"))),
            "queue_selection": selected.model_dump(mode="json"),
            "activity_protocol": int(dify_config.WORKBENCH_ACTIVITY_ENABLED and payload.get("activity_protocol") == 1),
            "template_snapshot_id": base["snapshot_id"],
            "is_continuation": silent_continue,
        }
        if silent_continue:
            # Values come from the owned persisted task, never this request's
            # draft. Execution still rechecks access to selected knowledge.
            for key in (
                "template_snapshot_id",
                "resource_mentions",
                "mentioned_resources",
                "mention_prompt",
                "sandbox_paths",
                "image_files",
                "queue_files",
                "inputs",
            ):
                if key in previous:
                    payload[key] = previous[key]
        run = WorkbenchRun(
            id=str(uuid4()),
            tenant_id=tenant_id,
            account_id=account_id,
            chat_id=chat.id,
            revision_id=revision.id,
            request_key=request_key,
            payload=json.dumps(payload),
            status=WAITING if defer else "queued",
            event_log="[]",
        )
        session.add(run)
        chat.updated_at = naive_utc_now()
        session.flush()
        if paused_parent is not None:
            previous = json.loads(paused_parent.payload)
            previous["user_paused"] = False
            previous["continued_by"] = run.id
            paused_parent.payload = json.dumps(previous)
            if waiting:
                first = waiting[0]
                queued_payload = json.loads(first.payload)
                queued_payload["branch_parent_run_id"] = run.id
                queued_payload["parent_message_id"] = None
                first.payload = json.dumps(queued_payload)
        dto = run_dto(run)
    from services.workbench.scheduler import publish

    if dto["status"] == WAITING:
        from services.workbench.followups import advance

        if advance(tenant_id, account_id, chat_id) == dto["id"]:
            with session_factory.create_session() as session:
                dto = run_dto(session.get(WorkbenchRun, dto["id"]))
    else:
        publish(tenant_id, account_id, dto["id"])
    return dto


def _prepare_message(tenant_id, account_id, chat_id, base, payload: dict[str, Any]):
    """Resolve new-message resources and files before acquiring the chat lock."""
    from services.workbench.mentions import default_capabilities, resolve_mentions

    # External provider discovery happens outside the write transaction.
    current = read_chat(tenant_id, account_id, chat_id)
    selected = default_capabilities(base["soul"], Selection.model_validate(current["selection"]))
    mention_data = resolve_mentions(base["soul"], payload.get("resource_mentions"))
    mentioned_tools = mention_data["resource_mentions"]["tools"]
    mentioned_skills = mention_data["resource_mentions"]["skills"]
    provider_names, skill_names = {}, {}
    if mentioned_tools:
        display_tools = [tool for tool in public_resources(base["soul"])["tools"] if tool["id"] in mentioned_tools]
        enrich_tool_labels(tenant_id, display_tools)
        provider_names = {tool["id"]: tool["provider_name"] for tool in display_tools if tool.get("provider_name")}
    if mentioned_skills:
        display_skills = [
            skill for skill in public_resources(base["soul"])["skills"] if skill["id"] in mentioned_skills
        ]
        enrich_skill_labels(tenant_id, base["agent_id"], display_skills)
        skill_names = {skill["id"]: skill["name"] for skill in display_skills}
    if mentioned_tools or mentioned_skills:
        mention_data = resolve_mentions(
            base["soul"], payload.get("resource_mentions"), provider_names=provider_names, skill_names=skill_names
        )
    selected.knowledge = list(dict.fromkeys([*selected.knowledge, *mention_data["resource_mentions"]["knowledge"]]))
    effective = compile_config(tenant_id, base, selected)
    payload = {**payload, **mention_data}
    if payload.get("files"):
        from services.workbench.files import validate_attachments

        paths, images = validate_attachments(tenant_id, account_id, chat_id, payload["files"])
        payload = {
            **payload,
            "queue_files": payload["files"],
            "sandbox_paths": paths,
            "image_files": images,
            "files": [],
        }
    # Retrieval can follow reading an attachment and use model-generated query
    # terms. Only a truly empty message is invalid.
    if not payload.get("query", "").strip() and not payload.get("sandbox_paths") and not payload.get("image_files"):
        raise Conflict("请输入消息或添加附件")
    return payload, current, selected, effective


def resolve_run_config(run_id, tenant_id, account_id):
    with session_factory.create_session() as session:
        run = session.scalar(
            select(WorkbenchRun).where(
                WorkbenchRun.id == run_id, WorkbenchRun.tenant_id == tenant_id, WorkbenchRun.account_id == account_id
            )
        )
        if run is None or run.status != "running":
            raise Forbidden()
        soul = AgentSoulConfig.model_validate(json.loads(run.payload)["effective_soul"])
    from services.workbench.knowledge import validate_run_knowledge

    validate_run_knowledge(tenant_id, account_id, soul)
    return soul


def resolve_run_generation(run_id, tenant_id, account_id):
    """Keep native Binding generation separate from the selected public resource revision."""
    with session_factory.create_session() as session:
        snapshot_id = session.scalar(
            select(WorkbenchChat.base_snapshot_id)
            .join(WorkbenchRun, WorkbenchRun.chat_id == WorkbenchChat.id)
            .where(
                WorkbenchRun.id == run_id,
                WorkbenchRun.status == "running",
                WorkbenchRun.tenant_id == tenant_id,
                WorkbenchRun.account_id == account_id,
                WorkbenchChat.tenant_id == tenant_id,
                WorkbenchChat.account_id == account_id,
                WorkbenchChat.deleted == 0,
            )
        )
        if snapshot_id is None:
            raise Forbidden()
        return snapshot_id


def delete_chat(tenant_id, account_id, chat_id):
    authorize(tenant_id, account_id)
    binding_id = None
    cancelled = []
    with session_factory.get_session_maker().begin() as session:
        chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
        runs = list(
            session.scalars(
                select(WorkbenchRun)
                .where(
                    WorkbenchRun.chat_id == chat.id,
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                )
                .with_for_update()
            )
        )
        if any(run.status in (*EXECUTING_STATUSES, "environment_update") for run in runs):
            raise Conflict("请先停止此会话的任务")
        from extensions.ext_redis import redis_client
        from services.workbench.scheduler import PREFIX

        if any(
            run.status != "waiting_input" and redis_client.zscore(PREFIX + "active", run.id) is not None for run in runs
        ):
            raise Conflict("任务仍在确认停止，请稍后再删除会话")
        from services.workbench.event_log import append_locked, uses_journal

        for run in runs:
            if run.status != "waiting_input":
                continue
            payload = json.loads(run.payload)
            payload.pop("pending", None)
            payload.pop("continuation", None)
            run.payload, run.status, run.error = json.dumps(payload), "cancelled", None
            if uses_journal(run):
                append_locked(session, run, {"event": "workbench_end", "status": "cancelled", "error": None})
            cancelled.append(run.id)
        from models.model import Conversation
        from services.agent.workspace_service import AgentWorkspaceService

        conversation = session.get(Conversation, chat.conversation_id) if chat.conversation_id else None
        if conversation is not None and conversation.app_id == chat.app_id:
            conversation.is_deleted = True
            if conversation.agent_workspace_binding_id:
                binding_id = AgentWorkspaceService.retire_binding(
                    session=session, tenant_id=tenant_id, binding_id=conversation.agent_workspace_binding_id
                )
                conversation.agent_workspace_binding_id = None
        chat.deleted = 1
    if cancelled:
        from tasks.workbench_tasks import force_stop

        for run_id in cancelled:
            redis_client.setex(PREFIX + "stop:" + run_id, 86400, "1")
            force_stop.delay(run_id, account_id)
    if binding_id:
        from tasks.collect_agent_resources_task import collect_agent_resources

        collect_agent_resources.apply_async(
            kwargs={"tenant_id": tenant_id, "binding_ids": [binding_id], "workspace_ids": [], "home_snapshot_ids": []},
            queue="workbench_control",
        )


def resume(tenant_id, account_id, run_id, values, action):
    authorize(tenant_id, account_id)
    from dify_agent.layers.ask_human.schema import AskHumanSelectedAction, AskHumanToolArgs, AskHumanToolResult

    if sum(len(key) + len(value) for key, value in values.items()) > 100000:
        raise ValueError("输入内容过长")
    with session_factory.get_session_maker().begin() as session:
        # Serialize deletion and resume in the same chat -> run lock order.
        chat_id = session.scalar(
            select(WorkbenchRun.chat_id).where(
                WorkbenchRun.id == run_id, WorkbenchRun.tenant_id == tenant_id, WorkbenchRun.account_id == account_id
            )
        )
        if chat_id is None:
            raise NotFound()
        _chat(session, tenant_id, account_id, chat_id, lock=True)
        run = session.scalar(
            select(WorkbenchRun)
            .where(
                WorkbenchRun.id == run_id, WorkbenchRun.tenant_id == tenant_id, WorkbenchRun.account_id == account_id
            )
            .with_for_update()
        )
        if run is None:
            raise NotFound()
        payload = json.loads(run.payload)
        if run.status != "waiting_input":
            if payload.get("submitted_input") == {"values": values, "action": action}:
                return run_dto(run)
            raise Conflict("此任务当前不在等待输入")
        pending = payload["pending"]
        args = AskHumanToolArgs.model_validate(pending["args"])
        allowed = {field.name: field for field in args.fields}
        if not set(values) <= allowed.keys():
            raise ValueError("输入包含未请求的字段")
        for field in args.fields:
            value = values.get(field.name)
            if field.required and not value:
                raise ValueError(f"请填写 {field.label}")
            if field.type == "select" and value is not None and value not in {option.value for option in field.options}:
                raise ValueError(f"{field.label} 选项无效")
        selected = None
        if action is not None:
            selected = next((item for item in args.actions if item.id == action), None)
            if selected is None:
                raise ValueError("操作选项无效")
        elif not args.fields and args.actions:
            raise ValueError("请选择操作")
        # Form answers stand on their own. Do not fabricate a separate action
        # that could contradict the user's selected field values.
        result = AskHumanToolResult(
            status="submitted",
            values=values,
            action=AskHumanSelectedAction(id=selected.id, label=selected.label) if selected else None,
        )
        payload["continuation"] = {"calls": {pending["tool_call_id"]: result.model_dump(mode="json")}}
        payload["submitted_input"] = {"values": values, "action": action}
        payload.pop("pending", None)
        payload.pop("human_input", None)
        payload["recovery"] = {"attempt": 0}
        payload["attempt"] = payload.get("attempt", 0) + 1
        run.payload, run.status, run.backend_run_id = json.dumps(payload), "queued", None
        dto = run_dto(run)
    from services.workbench.scheduler import publish

    publish(tenant_id, account_id, run_id)
    return dto
