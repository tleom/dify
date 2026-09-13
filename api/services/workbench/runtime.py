"""Trusted association between normal Dify conversations and workbench runs."""

import json

from sqlalchemy import select
from werkzeug.exceptions import Forbidden

from configs import dify_config
from core.db.session_factory import session_factory
from models.workbench import WorkbenchChat, WorkbenchRun


def sync_native_title(tenant_id, conversation_id):
    from services.workbench.titles import sync_native_title as sync

    if dify_config.WORKBENCH_ENABLED:
        sync(tenant_id, conversation_id)


def record_context_status(tenant_id, conversation_id, account_id, public_event):
    from services.workbench.context_status import record_context_status as record

    record(tenant_id, conversation_id, account_id, public_event)


def resolve_run_config(run_id, tenant_id, account_id):
    from services.workbench.service import resolve_run_config as resolve

    return resolve(run_id, tenant_id, account_id)


def resolve_run_generation(run_id, tenant_id, account_id):
    from services.workbench.service import resolve_run_generation as resolve

    return resolve(run_id, tenant_id, account_id)


def attach_conversation(run_id, tenant_id, account_id, conversation_id, task_id):
    with session_factory.get_session_maker().begin() as session:
        run = session.scalar(
            select(WorkbenchRun).where(
                WorkbenchRun.id == run_id, WorkbenchRun.tenant_id == tenant_id, WorkbenchRun.account_id == account_id
            )
        )
        if run is None:
            raise Forbidden()
        chat = session.get(WorkbenchChat, run.chat_id)
        if chat is None:
            raise Forbidden()
        if chat.conversation_id and chat.conversation_id != conversation_id:
            raise Forbidden()
        chat.conversation_id = conversation_id
        run.task_id = task_id
        from extensions.ext_redis import redis_client
        from services.workbench.scheduler import PREFIX

        redis_client.setex(PREFIX + "task:" + run_id, 86400, task_id)


def conversation_owner(tenant_id, conversation_id, account_id):
    if not dify_config.WORKBENCH_ENABLED:
        return None
    with session_factory.create_session() as session:
        return session.scalar(
            select(WorkbenchChat.account_id).where(
                WorkbenchChat.tenant_id == tenant_id,
                WorkbenchChat.conversation_id == conversation_id,
                WorkbenchChat.account_id == account_id,
                WorkbenchChat.deleted == 0,
            )
        )


def current_run(session, tenant_id, conversation_id, account_id):
    return session.scalar(
        select(WorkbenchRun)
        .join(WorkbenchChat, WorkbenchRun.chat_id == WorkbenchChat.id)
        .where(
            WorkbenchChat.tenant_id == tenant_id,
            WorkbenchChat.conversation_id == conversation_id,
            WorkbenchChat.account_id == account_id,
            WorkbenchChat.deleted == 0,
            WorkbenchRun.status == "running",
        )
    )


def execution_run_id(tenant_id, conversation_id, account_id):
    if not dify_config.WORKBENCH_ENABLED:
        return None
    with session_factory.create_session() as session:
        run = current_run(session, tenant_id, conversation_id, account_id)
        return run.id if run else None


def config_soul(session, *, run_id, tenant_id, account_id, agent_id, snapshot_id):
    """Resolve CLI assets from the immutable run selection, never from another user's task."""
    from models.agent_config_entities import AgentSoulConfig
    from services.agent_config_service import AgentConfigServiceError

    run = session.scalar(
        select(WorkbenchRun)
        .join(WorkbenchChat, WorkbenchRun.chat_id == WorkbenchChat.id)
        .where(
            WorkbenchRun.id == run_id,
            WorkbenchRun.tenant_id == tenant_id,
            WorkbenchRun.account_id == account_id,
            WorkbenchChat.tenant_id == tenant_id,
            WorkbenchChat.account_id == account_id,
            WorkbenchChat.agent_id == agent_id,
            WorkbenchChat.base_snapshot_id == snapshot_id,
            WorkbenchChat.deleted == 0,
        )
    )
    if not dify_config.WORKBENCH_ENABLED or run is None or run.status != "running":
        raise AgentConfigServiceError("config_access_denied", "Workbench run is no longer active", status_code=403)
    return AgentSoulConfig.model_validate(json.loads(run.payload)["effective_soul"])


def continuation(tenant_id, conversation_id, account_id):
    if not dify_config.WORKBENCH_ENABLED:
        return None
    with session_factory.create_session() as session:
        run = current_run(session, tenant_id, conversation_id, account_id)
        if run:
            value = json.loads(run.payload).get("continuation")
            if value:
                from dify_agent.protocol import DeferredToolResultsPayload

                return DeferredToolResultsPayload.model_validate(value)
    return None


def capture_run_history(tenant_id, account_id, conversation_id, run_id, snapshot):
    from services.workbench.history import history_state

    with session_factory.get_session_maker().begin() as session:
        run = session.scalar(
            select(WorkbenchRun)
            .join(WorkbenchChat, WorkbenchChat.id == WorkbenchRun.chat_id)
            .where(
                WorkbenchRun.id == run_id,
                WorkbenchRun.tenant_id == tenant_id,
                WorkbenchRun.account_id == account_id,
                WorkbenchChat.conversation_id == conversation_id,
                WorkbenchChat.tenant_id == tenant_id,
                WorkbenchChat.account_id == account_id,
            )
            .with_for_update()
        )
        if run is None:
            raise Forbidden()
        payload = json.loads(run.payload)
        payload["output_history"] = history_state(snapshot)
        run.payload = json.dumps(payload)


def prepare_execution(tenant_id, conversation_id, account_id, request):
    """Persist the remote ticket before any network call so recovery can revoke delayed requests."""
    if not dify_config.WORKBENCH_ENABLED:
        return
    from uuid import NAMESPACE_URL, uuid5

    from services.workbench.scheduler import heartbeat

    with session_factory.get_session_maker().begin() as session:
        run = current_run(session, tenant_id, conversation_id, account_id)
        if run is None:
            if conversation_owner(tenant_id, conversation_id, account_id):
                raise Forbidden("Workbench task was stopped")
            return
        if not heartbeat(f"{tenant_id}:{account_id}", run.id):
            raise Forbidden("Workbench execution lease expired")
        payload = json.loads(run.payload)
        if not payload.get("continuation"):
            parent = None
            from services.workbench.history import history_before_message, history_state, restore_history

            if "branch_parent_run_id" in payload:
                from services.workbench.branches import output_history

                parent_id = payload["branch_parent_run_id"]
                parent = session.get(WorkbenchRun, parent_id) if parent_id else None
                if parent_id and (
                    parent is None
                    or parent.chat_id != run.chat_id
                    or parent.account_id != account_id
                    or parent.tenant_id != tenant_id
                ):
                    raise Forbidden()
                request.session_snapshot = restore_history(
                    request.session_snapshot, output_history(session, parent) if parent else None
                )
            elif payload.get("regenerate_from"):
                from models.model import Message
                from services.workbench.message_actions import message_ids

                source = session.get(WorkbenchRun, payload["regenerate_from"])
                if source is None or source.chat_id != run.chat_id or source.account_id != account_id:
                    raise Forbidden()
                original = json.loads(source.payload)
                if "input_history" in original:
                    request.session_snapshot = restore_history(request.session_snapshot, original["input_history"])
                else:
                    ids = message_ids(source)
                    message = session.get(Message, ids[0]) if ids else None
                    if message is not None:
                        request.session_snapshot = history_before_message(
                            request.session_snapshot, message.query, message.created_at
                        )
            previous = (
                parent
                if "branch_parent_run_id" in payload
                else session.scalar(
                    select(WorkbenchRun)
                    .where(WorkbenchRun.chat_id == run.chat_id, WorkbenchRun.id != run.id)
                    .order_by(WorkbenchRun.created_at.desc())
                    .limit(1)
                )
            )
            if previous is not None and previous.status in ("cancelled", "failed", "interrupted"):
                from services.workbench.history import mark_interrupted_history

                request.session_snapshot = mark_interrupted_history(request.session_snapshot)
            payload["input_history"] = history_state(request.session_snapshot)
            run.payload = json.dumps(payload)
        ticket = str(uuid5(NAMESPACE_URL, f"dify-workbench-run:{run.id}:{json.loads(run.payload).get('attempt', 0)}"))
        run.backend_run_id = ticket
        request.execution_ticket = ticket


def pause(tenant_id, conversation_id, account_id, terminal, binding_id):
    if not dify_config.WORKBENCH_ENABLED:
        return False
    from extensions.ext_redis import redis_client
    from models.agent import AgentWorkspaceBinding
    from services.workbench.scheduler import PREFIX, event_key

    with session_factory.get_session_maker().begin() as session:
        run = current_run(session, tenant_id, conversation_id, account_id)
        if run is None:
            return False
        pending = terminal.deferred_tool_call.model_dump(mode="json")
        payload = json.loads(run.payload)
        payload["pending"] = pending
        payload.pop("continuation", None)
        run.payload = json.dumps(payload)
        binding = session.get(AgentWorkspaceBinding, binding_id)
        if binding is None or binding.tenant_id != tenant_id:
            raise Forbidden()
        binding.session_snapshot = terminal.session_snapshot.model_dump_json()
        run.status = "environment_update" if pending["tool_name"] == "update_shared_environment" else "waiting_input"
        run_id, status = run.id, run.status
        # Gate new user jobs before releasing this task's normal lease.
        if status == "environment_update":
            redis_client.set(PREFIX + f"maintenance:{tenant_id}:{account_id}", "1")
    redis_client.xadd(event_key(run_id), {"data": json.dumps({"event": "workbench_status", "status": status})})
    return True
