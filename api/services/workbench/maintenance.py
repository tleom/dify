"""Recover cancelled environment updates without reopening an active installer."""

import json
from uuid import UUID

from sqlalchemy import select

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


def cancelled_installations_finished(session, tenant_id, account_id, manager):
    from services.workbench.files import workspace_id

    runs = session.scalars(
        select(WorkbenchRun).where(
            WorkbenchRun.tenant_id == tenant_id,
            WorkbenchRun.account_id == account_id,
            WorkbenchRun.status.in_(["cancelled", "failed", "interrupted"]),
            WorkbenchRun.payload.contains("update_shared_environment"),
        )
    )
    for run in runs:
        payload = json.loads(run.payload)
        if payload.get("pending", {}).get("tool_name") != "update_shared_environment":
            continue
        # A cancelled conversation may still have a detached installer. Its stable
        # request ID lets the manager confirm completion without restarting it.
        result = manager(
            workspace_id(tenant_id, account_id),
            "environment-status",
            {"request_id": f"{run.id}:{payload.get('attempt', 0)}"},
        )
        if result.get("status") not in ("ready", "failed"):
            return False
    return True
