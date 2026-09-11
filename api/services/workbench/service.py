"""Account-scoped configuration transactions for the Agent workbench."""

from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import select
from werkzeug.exceptions import Conflict, Forbidden, NotFound

from configs import dify_config
from core.db.session_factory import session_factory
from models import Account, TenantAccountJoin
from models.agent import Agent, AgentConfigSnapshot
from models.agent_config_entities import AgentSoulConfig
from models.provider_ids import ModelProviderID
from models.workbench import WorkbenchChat, WorkbenchRevision, WorkbenchRun
from services.agent.roster_service import AgentRosterService
from services.model_provider_service import ModelProviderService
from services.workbench.catalog_labels import enrich_tool_labels
from services.workbench.policy import (
    Selection,
    compile_selection,
    prune_unavailable_selection,
    public_resources,
    template_resources,
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


def template(tenant_id: str, account_id: str):
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
        from controllers.common.rbac import AgentId, RBACCheck, RBACPermission, enforce_rbac_checks

        enforce_rbac_checks(
            tenant_id=tenant_id,
            account_id=account_id,
            checks=[RBACCheck(RBACPermission.AGENT_TEST_AND_RUN, AgentId())],
            path_args={"agent_id": agent.id},
        )
        base = {
            "agent_id": agent.id,
            "app_id": app.id,
            "snapshot_id": snapshot.id,
            "soul": snapshot.config_snapshot_dict,
        }
    from core.workflow.nodes.agent_v2.dify_tools_builder import WorkflowAgentDifyToolsBuilder

    soul = AgentSoulConfig.model_validate(base["soul"])
    expanded = WorkflowAgentDifyToolsBuilder().expand_provider_entries(
        tenant_id=tenant_id, enabled_tools=[tool for tool in soul.tools.dify_tools if tool.enabled]
    )
    base["soul"]["tools"]["dify_tools"] = [tool.model_dump(mode="json") for tool in expanded]
    # Freeze published workspace Skills as concrete archive references as well.
    from services.skill_management_service import SkillManagementService

    names = {item["name"] for item in base["soul"].get("config_skills", [])}
    for skill in SkillManagementService().list_runtime_agent_skills(tenant_id=tenant_id, agent_id=agent_id):
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
    return {
        **resources,
        "models": [
            {"id": key, "name": value["model"], "provider": value["model_provider"]} for key, value in models.items()
        ],
    }


def compile_config(tenant_id: str, base: dict, selection: Selection):
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
    from services.workbench.message_actions import with_feedback

    authorize(tenant_id, account_id)
    with session_factory.create_session() as session:
        chat = _chat(session, tenant_id, account_id, chat_id)
        revision = session.scalar(
            select(WorkbenchRevision).where(
                WorkbenchRevision.chat_id == chat.id, WorkbenchRevision.version == chat.version
            )
        )
        runs = list(
            session.scalars(
                select(WorkbenchRun)
                .where(WorkbenchRun.chat_id == chat.id)
                .order_by(WorkbenchRun.created_at, WorkbenchRun.id)
            )
        )
        return {
            "id": chat.id,
            "title": chat.title,
            "pinned": chat.pinned,
            "version": chat.version,
            "template_snapshot_id": revision.template_snapshot_id or chat.base_snapshot_id,
            "selection": json.loads(revision.selection),
            "runs": annotate(runs, with_feedback(session, runs, [run_dto(run) for run in runs])),
        }


def run_dto(run):
    from services.workbench.message_actions import message_ids
    from services.workbench.knowledge_events import run_knowledge_events

    payload = json.loads(run.payload)
    ids = message_ids(run)
    return {
        "id": run.id,
        "chat_id": run.chat_id,
        "revision_id": run.revision_id,
        "version": payload.get("version", 1),
        "pending": payload.get("pending"),
        "status": run.status,
        "error": run.error,
        "events": [*run_knowledge_events(run, payload), *json.loads(run.event_log)],
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
    authorize(tenant_id, account_id)
    with session_factory.create_session() as session:
        return [
            {"id": c.id, "title": c.title, "version": c.version, "pinned": c.pinned}
            for c in session.scalars(
                select(WorkbenchChat)
                .where(
                    WorkbenchChat.tenant_id == tenant_id,
                    WorkbenchChat.account_id == account_id,
                    WorkbenchChat.deleted == 0,
                )
                .order_by(WorkbenchChat.pinned.desc(), WorkbenchChat.updated_at.desc())
                .limit(200)
            )
        ]


def update_chat(tenant_id, account_id, chat_id, *, title=None, pinned=None):
    authorize(tenant_id, account_id)
    with session_factory.get_session_maker().begin() as session:
        chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
        if title is not None:
            chat.title = title
        if pinned is not None:
            chat.pinned = pinned
        return {"id": chat.id, "title": chat.title, "version": chat.version, "pinned": chat.pinned}


def create_chat(tenant_id, account_id):
    base = template(tenant_id, account_id)
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
                **{key: list(resources[key]) for key in resources},
            )
    from services.workbench.mentions import default_capabilities

    selection = prune_unavailable_selection(base["soul"], selection)
    selection = default_capabilities(base["soul"], selection, new_chat=True)
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


def enqueue(tenant_id, account_id, chat_id, version, request_key, payload):
    from services.workbench.mentions import default_capabilities, resolve_mentions

    base = template(tenant_id, account_id)
    # External provider discovery happens outside the write transaction.
    current = read_chat(tenant_id, account_id, chat_id)
    selected = default_capabilities(base["soul"], Selection.model_validate(current["selection"]))
    mention_data = resolve_mentions(base["soul"], payload.get("resource_mentions"))
    mentioned_tools = mention_data["resource_mentions"]["tools"]
    if mentioned_tools:
        display_tools = [tool for tool in public_resources(base["soul"])["tools"] if tool["id"] in mentioned_tools]
        enrich_tool_labels(tenant_id, display_tools)
        mention_data = resolve_mentions(base["soul"], payload.get("resource_mentions"), provider_names={
            tool["id"]: tool["provider_name"] for tool in display_tools if tool.get("provider_name")
        })
    selected.knowledge = list(dict.fromkeys([*selected.knowledge, *mention_data["resource_mentions"]["knowledge"]]))
    effective = compile_config(tenant_id, base, selected)
    if effective.get("knowledge", {}).get("sets") and not payload.get("query", "").strip():
        raise Conflict("请选择知识库后输入需要检索的问题")
    payload = {**payload, **mention_data}
    if payload.get("files"):
        from services.workbench.files import validate_attachments

        paths, images = validate_attachments(tenant_id, account_id, payload["files"])
        payload = {**payload, "sandbox_paths": paths, "image_files": images, "files": []}
    with session_factory.get_session_maker().begin() as session:
        chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
        existing = session.scalar(
            select(WorkbenchRun).where(WorkbenchRun.chat_id == chat.id, WorkbenchRun.request_key == request_key)
        )
        if existing:
            return run_dto(existing)
        if chat.version != version or current["version"] != version:
            raise Conflict("配置版本已改变，请刷新后发送")
        if chat.agent_id != base["agent_id"]:
            raise Conflict("管理员已切换通用 Agent，请新建会话")
        active = session.scalar(
            select(WorkbenchRun.id).where(
                WorkbenchRun.chat_id == chat.id,
                WorkbenchRun.status.in_(
                    ["queued", "running", "waiting_input", "environment_update", "environment_installing"]
                ),
            )
        )
        if active:
            raise Conflict("此会话已有任务，请等待完成或停止")
        revision = session.scalar(
            select(WorkbenchRevision).where(WorkbenchRevision.chat_id == chat.id, WorkbenchRevision.version == version)
        )
        # The task's effective configuration is frozen independently of future template edits.
        from services.workbench.branches import resolve_parent

        parent = resolve_parent(session, chat, payload)
        payload = {
            **payload,
            **parent,
            "effective_soul": effective,
            "version": version,
            "attempt": 0,
            "template_snapshot_id": base["snapshot_id"],
        }
        run = WorkbenchRun(
            id=str(uuid4()),
            tenant_id=tenant_id,
            account_id=account_id,
            chat_id=chat.id,
            revision_id=revision.id,
            request_key=request_key,
            payload=json.dumps(payload),
            status="queued",
            event_log="[]",
        )
        session.add(run)
        if chat.title == "新会话":
            chat.title = (payload["query"].strip() or payload["sandbox_paths"][0].rsplit("/", 1)[-1])[:80]
        session.flush()
        dto = run_dto(run)
    from services.workbench.scheduler import publish

    publish(tenant_id, account_id, dto["id"])
    return dto


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
    with session_factory.get_session_maker().begin() as session:
        chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
        active = session.scalar(
            select(WorkbenchRun.id).where(
                WorkbenchRun.chat_id == chat.id,
                WorkbenchRun.status.in_(
                    ["queued", "running", "waiting_input", "environment_update", "environment_installing"]
                ),
            )
        )
        if active:
            raise Conflict("请先停止此会话的任务")
        from extensions.ext_redis import redis_client
        from services.workbench.scheduler import PREFIX

        run_ids = session.scalars(select(WorkbenchRun.id).where(WorkbenchRun.chat_id == chat.id))
        if any(redis_client.zscore(PREFIX + "active", run_id) is not None for run_id in run_ids):
            raise Conflict("任务仍在确认停止，请稍后再删除会话")
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
        selected = next((item for item in args.actions if item.id == action), None)
        if selected is None:
            raise ValueError("操作选项无效")
        result = AskHumanToolResult(
            status="submitted", values=values, action=AskHumanSelectedAction(id=selected.id, label=selected.label)
        )
        payload["continuation"] = {"calls": {pending["tool_call_id"]: result.model_dump(mode="json")}}
        payload["submitted_input"] = {"values": values, "action": action}
        payload.pop("pending", None)
        payload["attempt"] = payload.get("attempt", 0) + 1
        run.payload, run.status, run.backend_run_id = json.dumps(payload), "queued", None
        dto = run_dto(run)
    from services.workbench.scheduler import publish

    publish(tenant_id, account_id, run_id)
    return dto
