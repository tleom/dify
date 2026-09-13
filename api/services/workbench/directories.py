"""Resolve file folders from owned chats; display names never become filesystem paths."""

from sqlalchemy import select
from werkzeug.exceptions import BadRequest, NotFound

from models.model import Conversation
from models.workbench import WorkbenchChat


def chat_directory(session, chat):
    binding = session.scalar(
        select(Conversation.agent_workspace_binding_id).where(
            Conversation.id == chat.conversation_id, Conversation.app_id == chat.app_id
        )
    ) if chat.conversation_id else None
    return "conversations/" + (binding or chat.id)


def owned_directories(session, tenant_id, account_id):
    chats = session.scalars(select(WorkbenchChat).where(
        WorkbenchChat.tenant_id == tenant_id, WorkbenchChat.account_id == account_id,
        WorkbenchChat.deleted == 0,
    ).order_by(WorkbenchChat.updated_at.desc()))
    return {chat_directory(session, chat): chat for chat in chats}


def resolve_path(session, tenant_id, account_id, path, *, chat_id=None):
    parts = path.split("/")
    if len(parts) < 2 or any(part in ("", ".", "..") or "\\" in part or "\x00" in part for part in parts):
        raise BadRequest("请选择对话文件夹中的文件")
    root = "/".join(parts[:2])
    chat = owned_directories(session, tenant_id, account_id).get(root)
    if chat is None or (chat_id is not None and chat.id != chat_id):
        raise NotFound("文件不属于当前对话")
    return root, chat


def archive_name(title):
    name = "".join(c for c in title if c not in '/\\:*?"<>|\x00\r\n').strip(" .")
    return (name[:120] or "对话文件") + ".zip"
