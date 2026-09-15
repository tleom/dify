"""Stable file capabilities shared by the workbench UI and its Agent.

The signature grants access to one owned path, not to a directory listing. Each
read rechecks the active chat and workspace; deleting either revokes the URL.
"""

import base64
import hashlib
import hmac
from pathlib import PurePosixPath
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from configs import dify_config
from core.db.session_factory import session_factory
from core.tools.signature import bind_file_uri
from models.agent import AgentWorkingResourceStatus, AgentWorkspace, AgentWorkspaceOwnerType
from models.workbench import WorkbenchChat, WorkbenchRun
from services.workbench.directories import resolve_path
from services.workbench.files import manager, operate


class FileClaims(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=64)
    account_id: str = Field(min_length=1, max_length=64)
    chat_id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=1024)


class AgentFileLinksPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    account_id: str
    app_id: str
    workbench_run_id: str
    path: str = Field(default=".", min_length=1, max_length=1024)


def _signature(encoded: str) -> str:
    return hmac.new(
        dify_config.SECRET_KEY.encode(), b"workbench-file-v1:" + encoded.encode(), hashlib.sha256
    ).hexdigest()


def links(tenant_id: str, account_id: str, chat_id: str, path: str) -> dict[str, str]:
    claims = FileClaims(tenant_id=tenant_id, account_id=account_id, chat_id=chat_id, path=path)
    encoded = base64.urlsafe_b64encode(claims.model_dump_json().encode()).decode().rstrip("=")
    suffix = f"/{encoded}.{_signature(encoded)}/{quote(PurePosixPath(path).name, safe='')}"
    url = bind_file_uri("/files/workbench" + suffix, dify_config.FILES_URL.rstrip("/"))
    return {"download_url": url + "?mode=download", "preview_url": url + "?mode=preview"}


def read_signed(token: str) -> dict:
    try:
        if len(token) > 8192:
            raise ValueError()
        encoded, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(_signature(encoded), signature):
            raise ValueError()
        claims = FileClaims.model_validate_json(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, UnicodeError) as error:
        raise NotFound("文件链接无效") from error
    with session_factory.create_session() as session:
        root, chat = resolve_path(session, claims.tenant_id, claims.account_id, claims.path, chat_id=claims.chat_id)
        workspace = session.scalar(
            select(AgentWorkspace).where(
                AgentWorkspace.tenant_id == claims.tenant_id,
                AgentWorkspace.owner_type == AgentWorkspaceOwnerType.WORKBENCH_USER,
                AgentWorkspace.owner_id == claims.account_id,
                AgentWorkspace.status == AgentWorkingResourceStatus.ACTIVE,
            )
        )
        if workspace is None:
            raise NotFound("文件空间已不可用")
        identifier, title = workspace.id, chat.title
    result = manager(identifier, "files", {"operation": "get", "path": claims.path})
    if result.get("kind") == "directory" and claims.path == root:
        from services.workbench.directories import archive_name

        result["name"] = archive_name(title)
    result.setdefault("name", PurePosixPath(claims.path).name)
    return result


def lookup(
    tenant_id: str,
    account_id: str,
    path: str,
    *,
    chat_id: str | None = None,
    require_downloadable: bool = True,
) -> dict:
    """Confirm the exact path independently of the bounded directory listing."""
    entry = operate(tenant_id, account_id, "stat", path, chat_id=chat_id)
    if entry["kind"] not in {"file", "directory"}:
        raise NotFound("文件已不存在或不可访问")
    if require_downloadable and not entry.get("downloadable", True):
        raise BadRequest("文件超出下载范围，请拆分过大的文件或移除目录内不支持的文件后重试")
    return entry


def agent_lookup(payload: AgentFileLinksPayload) -> dict:
    if not dify_config.WORKBENCH_ENABLED:
        raise Forbidden()
    with session_factory.create_session() as session:
        chat = session.scalar(
            select(WorkbenchChat)
            .join(WorkbenchRun, WorkbenchRun.chat_id == WorkbenchChat.id)
            .where(
                WorkbenchRun.id == payload.workbench_run_id,
                WorkbenchRun.tenant_id == payload.tenant_id,
                WorkbenchRun.account_id == payload.account_id,
                WorkbenchRun.status == "running",
                WorkbenchChat.tenant_id == payload.tenant_id,
                WorkbenchChat.account_id == payload.account_id,
                WorkbenchChat.app_id == payload.app_id,
                WorkbenchChat.deleted == 0,
            )
        )
        if chat is None:
            raise Forbidden("当前任务已结束或无法访问文件空间")
        from services.workbench.directories import chat_directory

        root, chat_id = chat_directory(session, chat), chat.id
    path = payload.path.removeprefix("/workspace/")
    if path == ".":
        path = root
    elif not path.startswith("conversations/"):
        path = root + "/" + path
    if path != root and not path.startswith(root + "/"):
        raise BadRequest("只能查询当前会话目录中的文件")
    if path == root:
        listing = operate(payload.tenant_id, payload.account_id, "list", path, chat_id=chat_id)
    else:
        item = lookup(payload.tenant_id, payload.account_id, path, chat_id=chat_id, require_downloadable=False)
        if item["kind"] == "file":
            return {"directory": root, "entries": [item], "complete": True}
        listing = operate(payload.tenant_id, payload.account_id, "list", path, chat_id=chat_id)
    entries = listing["entries"]
    # A specific path lookup can reach files beyond the bounded folder summary.
    return {"directory": path, "entries": entries[:200], "complete": len(entries) <= 200}
