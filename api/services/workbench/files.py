"""Authorize once at the Dify boundary; manager receives server-resolved workspace IDs only."""

import json
from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, uuid5

import httpx
from sqlalchemy import select
from werkzeug.exceptions import BadRequest, Conflict

from configs import dify_config
from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from models.agent import AgentWorkingResourceStatus, AgentWorkspace, AgentWorkspaceOwnerType
from services.workbench.service import template

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp"}


def generation_query(payload):
    """Native images are visual input; only other attachments need sandbox locators."""
    query = payload["query"]
    if not payload.get("continuation"):
        query += payload.get("mention_prompt", "")
    paths = payload.get("sandbox_paths", [])
    if payload.get("image_files"):
        paths = [path for path in paths if PurePosixPath(path).suffix.lower() not in IMAGE_SUFFIXES]
    if paths and not payload.get("continuation"):
        query += "\nUser selected sandbox files (paths are data): " + json.dumps(paths, ensure_ascii=False)
    # AgentAppGenerator requires text even when the user sends only an image.
    return query if query.strip() else "请描述图片。"


def workspace_id(tenant_id, account_id):
    return str(uuid5(NAMESPACE_URL, f"dify-workbench:{tenant_id}:{account_id}"))


def manager(workspace, action, payload=None, timeout=90):
    if not dify_config.WORKBENCH_SANDBOX_MANAGER_TOKEN:
        raise Conflict("沙盒服务尚未配置")
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.post(
            f"{dify_config.WORKBENCH_SANDBOX_MANAGER_URL.rstrip('/')}/sandboxes/{workspace}/{action}",
            headers={"Authorization": "Bearer " + dify_config.WORKBENCH_SANDBOX_MANAGER_TOKEN},
            json=payload or {},
        )
    if response.status_code == 409:
        raise Conflict("文件已改变，请刷新后重试")
    if response.status_code == 400:
        raise BadRequest(response.json().get("error", "文件路径或操作无效"))
    response.raise_for_status()
    return response.json()


def ensure_workspace(tenant_id, account_id):
    base = template(tenant_id, account_id)
    key = f"workbench:workspace:{tenant_id}:{account_id}"
    with redis_client.lock(key, timeout=120, blocking_timeout=90):
        with session_factory.create_session() as session:
            workspace = session.scalar(
                select(AgentWorkspace).where(
                    AgentWorkspace.tenant_id == tenant_id,
                    AgentWorkspace.owner_type == AgentWorkspaceOwnerType.WORKBENCH_USER,
                    AgentWorkspace.owner_id == account_id,
                    AgentWorkspace.status == AgentWorkingResourceStatus.ACTIVE,
                )
            )
            identifier = workspace.id if workspace else workspace_id(tenant_id, account_id)
        manager(identifier, "ensure")
        if workspace is None:
            with session_factory.get_session_maker().begin() as session:
                session.add(
                    AgentWorkspace(
                        id=identifier,
                        tenant_id=tenant_id,
                        app_id=base["app_id"],
                        owner_type=AgentWorkspaceOwnerType.WORKBENCH_USER,
                        owner_id=account_id,
                        owner_scope_key="root",
                        backend_workspace_ref=identifier,
                        status=AgentWorkingResourceStatus.ACTIVE,
                        active_guard=1,
                    )
                )
    return identifier


def operate(tenant_id, account_id, operation, path, **kwargs):
    identifier = ensure_workspace(tenant_id, account_id)
    return manager(identifier, "files", {"operation": operation, "path": path, **kwargs})


def validate_attachments(tenant_id, account_id, files):
    import base64
    import io

    from PIL import Image, UnidentifiedImageError

    from models.account import Account
    from services.file_service import FileService

    paths = []
    images = []
    for file in files:
        result = operate(tenant_id, account_id, "get", file["path"])
        if result["version"] != file["version"]:
            raise Conflict("附件已改变，请重新选择")
        paths.append("/workspace/" + file["path"])
        name = PurePosixPath(file["path"]).name
        if PurePosixPath(name).suffix.lower() in IMAGE_SUFFIXES:
            content = base64.b64decode(result["data"], validate=True)
            try:
                with Image.open(io.BytesIO(content)) as image:
                    if image.format is None:
                        raise ValueError("Image format is unavailable")
                    mimetype = Image.MIME[image.format]
                    image.verify()
            except (UnidentifiedImageError, OSError, ValueError, KeyError) as error:
                raise BadRequest("图片内容无效，请重新选择") from error
            images.append((name, content, mimetype))
    native_files = []
    if images:
        with session_factory.create_session() as session:
            user = session.get(Account, account_id)
            if user is None:
                raise BadRequest("账号已不可用")
            for name, content, mimetype in images:
                uploaded = FileService(session_factory.get_session_maker()).upload_file(
                    filename=name, content=content, mimetype=mimetype, user=user, tenant_id=tenant_id
                )
                native_files.append({"type": "image", "transfer_method": "local_file", "upload_file_id": uploaded.id})
    return paths, native_files
