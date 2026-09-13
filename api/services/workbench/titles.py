"""Mirror Dify's asynchronous conversation name without overwriting a user rename."""

import json

from sqlalchemy import select

from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from models.model import Conversation
from models.workbench import WorkbenchChat, WorkbenchRun
from services.workbench.scheduler import event_key


def sync_native_title(tenant_id, conversation_id):
    event = None
    with session_factory.get_session_maker().begin() as session:
        chat = session.scalar(select(WorkbenchChat).where(
            WorkbenchChat.tenant_id == tenant_id,
            WorkbenchChat.conversation_id == conversation_id,
            WorkbenchChat.deleted == 0,
        ).with_for_update())
        if chat is None or chat.title != "新会话":
            return
        native = session.scalar(select(Conversation).where(
            Conversation.id == conversation_id, Conversation.app_id == chat.app_id,
            Conversation.from_account_id == chat.account_id,
        ))
        if native is None or not native.name or native.name == "New conversation":
            return
        chat.title = native.name.strip()[:255]
        run_id = session.scalar(select(WorkbenchRun.id).where(
            WorkbenchRun.chat_id == chat.id, WorkbenchRun.tenant_id == tenant_id,
            WorkbenchRun.account_id == chat.account_id,
        ).order_by(WorkbenchRun.created_at.desc()).limit(1))
        if run_id:
            event = (run_id, {"event": "workbench_chat_title", "chat_id": chat.id, "title": chat.title})
    if event:
        redis_client.xadd(event_key(event[0]), {"data": json.dumps(event[1], ensure_ascii=False)}, maxlen=10000)
