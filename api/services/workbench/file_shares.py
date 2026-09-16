"""Owner-managed short links. A public capability grants one path and no directory listing."""

import hashlib
import re
import secrets
import time

from sqlalchemy import select
from werkzeug.exceptions import BadRequest, NotFound

from configs import dify_config
from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from models.agent import AgentWorkingResourceStatus, AgentWorkspace, AgentWorkspaceOwnerType
from models.workbench import WorkbenchFileShare
from services.workbench.directories import resolve_path
from services.workbench.file_links import lookup
from services.workbench.files import ensure_workspace, manager


def _query(tenant_id: str, account_id: str, path: str):
    return select(WorkbenchFileShare).where(
        WorkbenchFileShare.tenant_id == tenant_id,
        WorkbenchFileShare.account_id == account_id,
        WorkbenchFileShare.path_hash == hashlib.sha256(path.encode()).hexdigest(),
    )


def _active(share: WorkbenchFileShare) -> bool:
    return not share.revoked and (share.expires_at is None or share.expires_at > int(time.time()))


def _dto(share: WorkbenchFileShare) -> dict:
    return {
        "url": dify_config.FILES_URL.rstrip("/") + "/files/s/" + share.token,
        "expires_at": share.expires_at,
        "active": _active(share),
    }


def get_share(tenant_id: str, account_id: str, path: str) -> dict | None:
    with session_factory.create_session() as session:
        resolve_path(session, tenant_id, account_id, path)
        share = session.scalar(_query(tenant_id, account_id, path))
        return _dto(share) if share and _active(share) else None


def create_share(tenant_id: str, account_id: str, path: str, expires_days: int | None = None) -> dict:
    if expires_days not in {None, 1, 7, 30}:
        raise BadRequest("分享有效期无效")
    entry = lookup(tenant_id, account_id, path)
    if entry["kind"] != "file":
        raise BadRequest("请选择单个文件进行分享")
    identifier = ensure_workspace(tenant_id, account_id)
    digest = hashlib.sha256(path.encode()).hexdigest()
    with redis_client.lock(f"workbench:share:{tenant_id}:{account_id}:{digest}", timeout=15, blocking_timeout=10):
        with session_factory.get_session_maker().begin() as session:
            resolve_path(session, tenant_id, account_id, path)
            share = session.scalar(_query(tenant_id, account_id, path).with_for_update())
            if share is None:
                share = WorkbenchFileShare(tenant_id=tenant_id, account_id=account_id, path=path, path_hash=digest)
                session.add(share)
            share.workspace_id = identifier
            share.token = secrets.token_urlsafe(18)
            share.expires_at = int(time.time()) + expires_days * 86400 if expires_days is not None else None
            share.revoked = False
            session.flush()
            return _dto(share)


def revoke_share(tenant_id: str, account_id: str, path: str) -> dict:
    with session_factory.get_session_maker().begin() as session:
        share = session.scalar(_query(tenant_id, account_id, path).with_for_update())
        if share:
            share.revoked = True
    return {"deleted": True}


def read_share(token: str) -> dict:
    if not dify_config.WORKBENCH_ENABLED or not re.fullmatch(r"[A-Za-z0-9_-]{24}", token):
        raise NotFound("分享链接不存在或已失效")
    with session_factory.create_session() as session:
        share = session.scalar(select(WorkbenchFileShare).where(WorkbenchFileShare.token == token))
        if share is None or not _active(share):
            raise NotFound("分享链接不存在或已失效")
        resolve_path(session, share.tenant_id, share.account_id, share.path)
        workspace = session.scalar(
            select(AgentWorkspace).where(
                AgentWorkspace.id == share.workspace_id,
                AgentWorkspace.tenant_id == share.tenant_id,
                AgentWorkspace.owner_type == AgentWorkspaceOwnerType.WORKBENCH_USER,
                AgentWorkspace.owner_id == share.account_id,
                AgentWorkspace.status == AgentWorkingResourceStatus.ACTIVE,
            )
        )
        if workspace is None:
            raise NotFound("分享链接不存在或已失效")
        identifier, path = workspace.id, share.path
    result = manager(identifier, "files", {"operation": "get", "path": path})
    if result.get("kind") == "directory":
        raise NotFound("分享文件已不可用")
    return result
