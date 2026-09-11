"""Authorize once at the Dify boundary; manager receives server-resolved workspace IDs only."""

from uuid import NAMESPACE_URL, uuid5

import httpx
from sqlalchemy import select
from werkzeug.exceptions import BadRequest, Conflict

from configs import dify_config
from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from models.agent import AgentWorkingResourceStatus, AgentWorkspace, AgentWorkspaceOwnerType
from services.workbench.service import template


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
    paths = []
    for file in files:
        result = operate(tenant_id, account_id, "get", file["path"])
        if result["version"] != file["version"]:
            raise Conflict("附件已改变，请重新选择")
        paths.append("/workspace/" + file["path"])
    return paths
