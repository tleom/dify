"""Use native Dify message parents while retaining the workbench execution history."""
import json

from sqlalchemy import select
from werkzeug.exceptions import Conflict, NotFound

from models.account import Account
from models.model import App
from models.workbench import WorkbenchRun
from services.message_service import MessageService


def parent_links(runs):
    """Read persisted branch relationships."""
    ids = {run.id for run in runs}
    return {run.id: parent if (parent := json.loads(run.payload).get("branch_parent_run_id")) in ids else None
            for run in runs}


def chat_runs(session, chat):
    return list(session.scalars(select(WorkbenchRun).where(
        WorkbenchRun.chat_id == chat.id, WorkbenchRun.tenant_id == chat.tenant_id,
        WorkbenchRun.account_id == chat.account_id,
    ).order_by(WorkbenchRun.created_at, WorkbenchRun.id)))


def annotate(runs, dtos):
    from services.workbench.message_actions import message_ids
    links = parent_links(runs)
    messages = {run.id: (message_ids(run) or [None])[-1] for run in runs}
    for dto in dtos:
        dto["parent_run_id"] = links[dto["id"]]
        dto["parent_message_id"] = messages.get(links[dto["id"]])
    return dtos


def resolve_parent(session, chat, payload):
    from services.workbench.message_actions import message_ids
    runs = chat_runs(session, chat)
    if payload.get("regenerate_from"):
        source = next((run for run in runs if run.id == payload["regenerate_from"]), None)
        if source is None:
            raise NotFound()
        parent_id = parent_links(runs)[source.id]
        parent = next((run for run in runs if run.id == parent_id), None)
    elif "parent_message_id" in payload:
        native_id = payload["parent_message_id"]
        if native_id is None:
            parent = None
        else:
            app = session.get(App, chat.app_id)
            user = session.get(Account, chat.account_id)
            message = MessageService.get_message(app_model=app, user=user, message_id=native_id, session=session)
            if message.conversation_id != chat.conversation_id or app.tenant_id != chat.tenant_id:
                raise NotFound()
            parent = next((run for run in runs if native_id in message_ids(run)), None)
            if parent is None:
                raise NotFound()
    else:
        parent = runs[-1] if runs else None
    if parent and parent.status not in {"completed", "failed", "cancelled", "interrupted"}:
        raise Conflict("请等待所选版本执行结束")
    return {"branch_parent_run_id": parent.id if parent else None,
            "parent_message_id": (message_ids(parent) or [None])[-1] if parent else None}


def output_history(session, parent):
    """Read the selected run's captured native runtime history."""
    data = json.loads(parent.payload)
    if "output_history" in data:
        return data["output_history"]
    if parent.status in {"failed", "cancelled", "interrupted"}:
        return data.get("input_history")
    raise Conflict("该版本的上下文尚未保存，请重新生成后继续")
