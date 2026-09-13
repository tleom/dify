"""Recover cancelled environment updates without reopening an active installer."""

import json
from uuid import UUID

from sqlalchemy import select

from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from models.workbench import WorkbenchRun
from services.workbench import scheduler


def gated_owners():
    prefix = scheduler.PREFIX + "maintenance:"
    owners = set()
    for key in redis_client.scan_iter(match=prefix + "*", count=100):
        key = key.decode() if isinstance(key, bytes) else key
        try:
            tenant_id, account_id = key.removeprefix(prefix).split(":")
            UUID(tenant_id)
            UUID(account_id)
        except ValueError:
            continue
        owners.add((tenant_id, account_id))
    return owners


def cancelled_installation_requests(session, tenant_id, account_id):
    runs = session.scalars(
        select(WorkbenchRun).where(
            WorkbenchRun.tenant_id == tenant_id,
            WorkbenchRun.account_id == account_id,
            WorkbenchRun.status.in_(["cancelled", "failed", "interrupted"]),
            WorkbenchRun.payload.contains("update_shared_environment"),
        )
    )
    requests = []
    for run in runs:
        payload = json.loads(run.payload)
        if payload.get("pending", {}).get("tool_name") != "update_shared_environment":
            continue
        requests.append(f"{run.id}:{payload.get('attempt', 0)}")
    return requests


def cancelled_installations_finished(tenant_id, account_id, manager):
    from services.workbench.files import workspace_id

    with session_factory.create_session() as session:
        requests = cancelled_installation_requests(session, tenant_id, account_id)
    for request_id in requests:
        # A cancelled conversation may still have a detached installer. Its stable
        # request ID lets the manager confirm completion without restarting it.
        result = manager(
            workspace_id(tenant_id, account_id),
            "environment-status",
            {"request_id": request_id},
        )
        if result.get("status") not in ("ready", "failed"):
            return False
    return True
