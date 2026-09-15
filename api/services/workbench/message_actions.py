"""Use Dify messages and feedback for workbench response actions."""

import json

from sqlalchemy import select
from werkzeug.exceptions import Conflict, NotFound

from core.db.session_factory import session_factory
from models import Account
from models.enums import FeedbackRating
from models.model import App, Message, MessageFeedback
from models.workbench import WorkbenchChat, WorkbenchRun
from services.message_service import MessageService
from services.workbench import service


def message_ids(run):
    payload = json.loads(run.payload)
    if payload.get("activity_protocol") == 1:
        return payload.get("message_ids", [])
    return list(
        dict.fromkeys(
            item["message_id"]
            for item in json.loads(run.event_log)
            if isinstance(item.get("message_id"), str) and item["message_id"]
        )
    )


def with_feedback(session, runs, dtos):
    ids = {dto.get("message_id") for dto in dtos} - {None}
    owners = {run.account_id for run in runs}
    ratings = (
        dict(
            session.execute(
                select(MessageFeedback.message_id, MessageFeedback.rating)
                .join(Message, Message.id == MessageFeedback.message_id)
                .where(
                    MessageFeedback.message_id.in_(ids),
                    MessageFeedback.from_account_id.in_(owners),
                    Message.from_account_id == MessageFeedback.from_account_id,
                )
            ).all()
        )
        if ids
        else {}
    )
    for dto in dtos:
        dto["feedback"] = ratings.get(dto.get("message_id"))
    return dtos


def _owned(session, tenant_id, account_id, run_id):
    run = session.scalar(
        select(WorkbenchRun)
        .join(WorkbenchChat, WorkbenchChat.id == WorkbenchRun.chat_id)
        .where(
            WorkbenchRun.id == run_id,
            WorkbenchRun.tenant_id == tenant_id,
            WorkbenchRun.account_id == account_id,
            WorkbenchChat.tenant_id == tenant_id,
            WorkbenchChat.account_id == account_id,
            WorkbenchChat.deleted == 0,
        )
    )
    if run is None:
        raise NotFound()
    return run, session.get(WorkbenchChat, run.chat_id)


def feedback(tenant_id, account_id, run_id, rating):
    service.authorize(tenant_id, account_id)
    with session_factory.create_session() as session:
        run, chat = _owned(session, tenant_id, account_id, run_id)
        ids = message_ids(run)
        if not ids:
            raise Conflict("这条结果还没有可评价的消息")
        app_model = session.get(App, chat.app_id)
        user = session.get(Account, account_id)
        if app_model is None or app_model.tenant_id != tenant_id or user is None:
            raise NotFound()
        message = MessageService.get_message(app_model=app_model, user=user, message_id=ids[-1], session=session)
        if message.conversation_id != chat.conversation_id or app_model.tenant_id != tenant_id:
            raise NotFound()
        if rating is not None or message.admin_feedback_with_session(session=session) is not None:
            MessageService.create_feedback(
                app_model=app_model,
                message_id=message.id,
                user=user,
                rating=FeedbackRating(rating) if rating else None,
                content=None,
                session=session,
            )
        return with_feedback(session, [run], [service.run_dto(run)])[0]


def regenerate(tenant_id, account_id, run_id, version, request_key, *, query=None, activity_protocol=0):
    service.authorize(tenant_id, account_id)
    with session_factory.create_session() as session:
        run, chat = _owned(session, tenant_id, account_id, run_id)
        if run.status not in ("completed", "failed", "cancelled", "interrupted"):
            raise Conflict("请先等待任务结束或停止任务")
        original = json.loads(run.payload)
        if query is not None and not query.strip() and not original.get("sandbox_paths"):
            raise Conflict("请输入问题或保留附件")
        ids = message_ids(run)
        message = session.get(Message, ids[0]) if ids else None
        if message is not None and (message.app_id != chat.app_id or message.from_account_id != account_id):
            raise NotFound()
        payload = {
            "activity_protocol": activity_protocol,
            # New native runs support steering independently of whether a
            # regeneration may queue behind another active task.
            "followup_protocol": 1,
            "query": original["query"] if query is None else query,
            "edited_from": run.id if query is not None else None,
            "inputs": original.get("inputs", {}),
            "files": [],
            "sandbox_paths": original.get("sandbox_paths", []),
            "image_files": original.get("image_files", []),
            "resource_mentions": original.get("resource_mentions", {}),
            "regenerate_from": run.id,
            "parent_message_id": message.parent_message_id if message else None,
        }
        chat_id = chat.id
    return service.enqueue(tenant_id, account_id, chat_id, version, request_key, payload)
