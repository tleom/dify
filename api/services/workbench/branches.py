"""Resolve owned workbench parents, including stopped runs without native messages."""

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
    return {
        run.id: parent if (parent := json.loads(run.payload).get("branch_parent_run_id")) in ids else None
        for run in runs
    }


def chat_runs(session, chat):
    return list(
        session.scalars(
            select(WorkbenchRun)
            .where(
                WorkbenchRun.chat_id == chat.id,
                WorkbenchRun.tenant_id == chat.tenant_id,
                WorkbenchRun.account_id == chat.account_id,
                WorkbenchRun.status.not_in(("discarded", "steered")),
            )
            .order_by(WorkbenchRun.created_at, WorkbenchRun.id)
        )
    )


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
    elif "parent_run_id" in payload:
        parent_id = payload["parent_run_id"]
        parent = next((run for run in runs if run.id == parent_id), None)
        if parent_id is not None and parent is None:
            raise NotFound()
        native_id = payload.get("parent_message_id")
        if native_id is not None and (parent is None or native_id not in message_ids(parent)):
            raise NotFound()
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
    return {
        "branch_parent_run_id": parent.id if parent else None,
        "parent_message_id": (message_ids(parent) or [None])[-1] if parent else None,
    }


def output_history(session, parent):
    """Read the selected run's captured native runtime history."""
    seen = set()
    from services.workbench.followups import carry_unseen_history

    parents = []
    while parent.id not in seen:
        seen.add(parent.id)
        data = json.loads(parent.payload)
        parents.insert(0, data)
        if "output_history" in data:
            return carry_unseen_history(data["output_history"], parents)
        if parent.status not in {"failed", "cancelled", "interrupted"}:
            break
        if "input_history" in data:
            return carry_unseen_history(data["input_history"], parents)
        # A task stopped in the queue has no snapshot; retain its selected ancestor.
        parent_id = data.get("branch_parent_run_id")
        if not parent_id:
            return carry_unseen_history(None, parents)
        ancestor = session.scalar(
            select(WorkbenchRun).where(
                WorkbenchRun.id == parent_id,
                WorkbenchRun.chat_id == parent.chat_id,
                WorkbenchRun.tenant_id == parent.tenant_id,
                WorkbenchRun.account_id == parent.account_id,
            )
        )
        if ancestor is None:
            raise NotFound()
        parent = ancestor
    raise Conflict("该版本的上下文尚未保存，请重新生成后继续")
