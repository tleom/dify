"""Durable workbench queue, isolated execution, and deferred environment maintenance."""

import json
import logging
import threading
import time

import httpx
from celery import shared_task
from flask import current_app
from sqlalchemy import select, update

from configs import dify_config
from core.app.apps.agent_app.app_generator import AgentAppGenerator
from core.app.entities.app_invoke_entities import InvokeFrom
from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from models import Account
from models.model import App, AppMode
from models.workbench import WorkbenchChat, WorkbenchRun
from services.app_task_service import AppTaskService
from services.workbench import maintenance, scheduler
from services.workbench.service import authorize

logger = logging.getLogger(__name__)


def event(run_id, payload):
    identifier = redis_client.xadd(scheduler.event_key(run_id), {"data": json.dumps(payload)})
    redis_client.expire(scheduler.event_key(run_id), 7 * 86400)
    return identifier


def stop_native(run_id, account_id):
    task_id = redis_client.get(scheduler.PREFIX + "task:" + run_id)
    if task_id:
        task_id = task_id.decode() if isinstance(task_id, bytes) else task_id
        AppTaskService.stop_task(task_id, InvokeFrom.EXPLORE, account_id, AppMode.AGENT)


def fence_remote(ticket):
    """Do not release capacity until cancellation has reached a terminal remote state."""
    if not ticket:
        return True
    headers = {"Authorization": "Bearer " + dify_config.AGENT_BACKEND_API_TOKEN}
    with httpx.Client(
        base_url=dify_config.AGENT_BACKEND_BASE_URL, headers=headers, timeout=10, trust_env=False
    ) as client:
        result = client.post(f"/runs/{ticket}/fence", json={})
        result.raise_for_status()
        # running means the remote owner still needs to finish cleanup.
        return result.json()["status"] != "running"


@shared_task(queue="workbench_control")
def force_stop(run_id, account_id):
    """Cancel the remote runner directly; cleanup does not block the composer."""
    from uuid import NAMESPACE_URL, uuid5

    stop_native(run_id, account_id)
    with session_factory.create_session() as session:
        run = session.get(WorkbenchRun, run_id)
        if run is None or run.account_id != account_id:
            return
        ticket = run.backend_run_id or str(
            uuid5(NAMESPACE_URL, f"dify-workbench-run:{run.id}:{json.loads(run.payload).get('attempt', 0)}")
        )
        owner = f"{run.tenant_id}:{account_id}"
    try:
        if fence_remote(ticket):
            scheduler.release(owner, run_id)
    except Exception:
        logger.warning("Remote cancellation will be retried: %s", run_id, exc_info=True)
    event(run_id, {"event": "workbench_end", "status": "cancelled", "error": None})
    reconcile.delay()


@shared_task(queue="workbench_control")
def dispatch():
    if not dify_config.WORKBENCH_ENABLED:
        return
    for _ in range(dify_config.WORKBENCH_GLOBAL_RUNS):
        claimed = scheduler.claim()
        if not claimed:
            break
        execute.apply_async(args=claimed, task_id="workbench-" + claimed[1])


@shared_task(queue="workbench_control")
def reconcile():
    """Revoke ambiguous execution tickets; never replay an uncertain side effect."""
    if not dify_config.WORKBENCH_ENABLED:
        return
    expired = [
        item.decode() if isinstance(item, bytes) else item
        for item in redis_client.zrangebyscore(scheduler.PREFIX + "active", "-inf", time.time())
    ]
    # An executor disconnect can finish the local stream before the admission lease
    # expires. Retry its cleanup immediately after the executor comes back; waiting
    # for the lease deadline would leave orphaned Shell jobs running unnecessarily.
    active = [
        item.decode() if isinstance(item, bytes) else item
        for item in redis_client.zrange(scheduler.PREFIX + "active", 0, -1)
    ]
    with session_factory.create_session() as session:
        terminal = (
            list(
                session.scalars(
                    select(WorkbenchRun.id).where(
                        WorkbenchRun.id.in_(active),
                        WorkbenchRun.status.in_(["failed", "cancelled", "interrupted", "completed"]),
                    )
                )
            )
            if active
            else []
        )
    for run_id in dict.fromkeys([*expired, *terminal]):
        with session_factory.get_session_maker().begin() as session:
            run = session.get(WorkbenchRun, run_id)
            if run is None:
                continue
            owner = f"{run.tenant_id}:{run.account_id}"
            ticket = run.backend_run_id
            if run.status in ("running", "queued"):
                run.status = "interrupted"
                run.error = "执行进程失联，任务不会自动重做。请核对外部操作结果。"
            account_id = run.account_id
        stop_native(run_id, account_id)
        try:
            if fence_remote(ticket):
                scheduler.release(owner, run_id)
        except Exception:
            logger.warning("Workbench ticket remains quarantined: %s", run_id, exc_info=True)
    with session_factory.create_session() as session:
        for run in session.scalars(select(WorkbenchRun).where(WorkbenchRun.status == "queued")):
            if redis_client.zscore(scheduler.PREFIX + "active", run.id) is None:
                redis_client.eval(scheduler.PUBLISH, 1, scheduler.PREFIX, f"{run.tenant_id}:{run.account_id}", run.id)
        owners = {
            (run.tenant_id, run.account_id)
            for run in session.scalars(
                select(WorkbenchRun).where(WorkbenchRun.status.in_(["environment_update", "environment_installing"]))
            )
        }
    for tenant_id, account_id in owners:
        redis_client.set(scheduler.PREFIX + f"maintenance:{tenant_id}:{account_id}", "1")
    # Cancellation changes the DB status but must not orphan the Redis gate.
    for tenant_id, account_id in owners | maintenance.gated_owners():
        update_environment.delay(tenant_id, account_id)
    dispatch.delay()


@shared_task(queue="workbench", acks_late=False, reject_on_worker_lost=False)
def execute(owner, run_id):
    tenant_id, account_id = owner.split(":", 1)
    # A newly submitted turn may queue immediately while its predecessor cleans up.
    with session_factory.create_session() as session:
        current = session.get(WorkbenchRun, run_id)
        if current is not None and current.status == "queued":
            previous_ids = session.scalars(
                select(WorkbenchRun.id).where(
                    WorkbenchRun.chat_id == current.chat_id,
                    WorkbenchRun.created_at < current.created_at,
                )
            )
            if any(redis_client.zscore(scheduler.PREFIX + "active", prior) is not None for prior in previous_ids):
                if scheduler.heartbeat(owner, run_id):
                    execute.apply_async(args=[owner, run_id], countdown=1)
                return
    done = threading.Event()
    claimed = False
    completed_stream = False
    events = []
    status, error = "completed", None
    app = current_app._get_current_object()
    try:
        with session_factory.get_session_maker().begin() as session:
            claimed = session.execute(
                update(WorkbenchRun)
                .where(
                    WorkbenchRun.id == run_id,
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                    WorkbenchRun.status == "queued",
                )
                .values(status="running")
            ).rowcount
            if not claimed:
                return
            run = session.get(WorkbenchRun, run_id)
            chat = session.get(WorkbenchChat, run.chat_id)
            payload, conversation_id, app_id = json.loads(run.payload), chat.conversation_id, chat.app_id
            events = json.loads(run.event_log)
        authorize(tenant_id, account_id)
        if not scheduler.heartbeat(owner, run_id):
            raise RuntimeError("Admission lease expired before execution")
        event(run_id, {"event": "workbench_status", "status": "running"})

        def renew():
            while not done.wait(5):
                try:
                    leased = scheduler.heartbeat(owner, run_id)
                    if not leased or redis_client.get(scheduler.PREFIX + "stop:" + run_id):
                        with app.app_context():
                            stop_native(run_id, account_id)
                except Exception:
                    logger.exception("Workbench heartbeat failed")
                    with app.app_context():
                        stop_native(run_id, account_id)

        threading.Thread(target=renew, daemon=True).start()
        with app.test_request_context():
            with session_factory.create_session() as session:
                user = session.get(Account, account_id)
                app_model = session.get(App, app_id)
                if user is None or app_model is None or app_model.tenant_id != tenant_id:
                    raise ValueError("会话所属应用已不可用")
                user.set_tenant_id_with_session(tenant_id, session=session)
                from services.workbench.files import generation_query

                query = generation_query(payload)
                result = AgentAppGenerator().generate(
                    app_model=app_model,
                    user=user,
                    session=session,
                    args={
                        "inputs": payload.get("inputs", {}),
                        "query": query,
                        "files": payload.get("image_files", []) if not payload.get("continuation") else [],
                        "conversation_id": conversation_id,
                        "workbench_run_id": run_id,
                        **(
                            {"parent_message_id": payload["parent_message_id"]}
                            if "parent_message_id" in payload
                            else {}
                        ),
                    },
                    invoke_from=InvokeFrom.EXPLORE,
                    streaming=True,
                )
                session.close()
                buffer = ""
                for chunk in result:
                    if isinstance(chunk, dict):
                        frames = [chunk]
                    else:
                        buffer += chunk.decode() if isinstance(chunk, bytes) else chunk
                        frames = []
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            data = "\n".join(
                                line[5:].strip() for line in frame.splitlines() if line.startswith("data:")
                            )
                            if data and data != "[DONE]":
                                frames.append(json.loads(data))
                    for item in frames:
                        item["workbench_run_id"] = run_id
                        events.append(item)
                        cursor = event(run_id, item)
                        item["_id"] = cursor.decode() if isinstance(cursor, bytes) else str(cursor)
                        if item.get("event") == "error":
                            status, error = "failed", item.get("message", "Agent 执行失败")
                completed_stream = True
    except Exception:
        logger.exception("Workbench run failed: %s", run_id)
        status, error = "failed", "Agent 执行失败，请查看管理员日志后重试"
    finally:
        done.set()
        if claimed:
            with session_factory.get_session_maker().begin() as session:
                run = session.get(WorkbenchRun, run_id)
                ticket = run.backend_run_id
                if redis_client.get(scheduler.PREFIX + "stop:" + run_id):
                    status, error = "cancelled", None
                    run.status = status
                elif run.status in ("environment_update", "waiting_input"):
                    status = run.status
                elif run.status == "running":
                    run.status, run.error = status, error
                else:
                    status, error = run.status, run.error
                run.event_log = json.dumps(events)
            safe_to_release = completed_stream and status in ("completed", "environment_update", "waiting_input")
            if not safe_to_release:
                try:
                    safe_to_release = fence_remote(ticket)
                except Exception:
                    logger.warning("Could not confirm remote cleanup for %s", run_id, exc_info=True)
            if safe_to_release:
                scheduler.release(owner, run_id)
            event(run_id, {"event": "workbench_end", "status": status, "error": error})
            if status == "environment_update":
                update_environment.delay(tenant_id, account_id)
            dispatch.delay()
        else:
            # A late delivery for a cancelled/finished task must not leak its reservation.
            with session_factory.create_session() as session:
                run = session.get(WorkbenchRun, run_id)
                if run is not None and run.status not in ("queued", "running"):
                    scheduler.release(owner, run_id)


@shared_task(queue="workbench_environment", acks_late=False)
def update_environment(tenant_id, account_id):
    from services.workbench.files import ensure_workspace, manager

    owner = f"{tenant_id}:{account_id}"
    maintenance_key = scheduler.PREFIX + "maintenance:" + owner
    with redis_client.lock(scheduler.PREFIX + "environment-lock:" + owner, timeout=1200, blocking_timeout=1):
        if redis_client.zcard(scheduler.PREFIX + "active:" + owner):
            return
        with session_factory.get_session_maker().begin() as session:
            run = session.scalar(
                select(WorkbenchRun)
                .where(
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                    WorkbenchRun.status.in_(["environment_update", "environment_installing"]),
                )
                .order_by(WorkbenchRun.created_at)
                .with_for_update()
                .limit(1)
            )
            if run is not None:
                run_id, previous_status, payload = run.id, run.status, json.loads(run.payload)
                run.status = "environment_installing"
        if run is None:
            try:
                if not maintenance.cancelled_installations_finished(tenant_id, account_id, manager):
                    return
            except Exception:
                logger.warning("Cancelled installer cleanup will be retried: %s", owner, exc_info=True)
                return
            redis_client.delete(maintenance_key)
            dispatch.delay()
            return
        result = None
        try:
            if previous_status != "environment_installing":
                authorize(tenant_id, account_id)
            workspace = ensure_workspace(tenant_id, account_id)
            request_id = f"{run_id}:{payload.get('attempt', 0)}"
            if previous_status == "environment_installing":
                result = manager(workspace, "environment-status", {"request_id": request_id})
                if result["status"] == "installing":
                    return
            else:
                event(run_id, {"event": "workbench_status", "status": "environment_installing"})
                result = manager(
                    workspace, "environment", {**payload["pending"]["args"], "request_id": request_id}, timeout=950
                )
                if result["status"] == "installing":
                    return
        except Exception:
            logger.exception("Workbench environment update failed")
            # A lost response is not evidence that the installer stopped. Reconcile by request ID.
            return
        with session_factory.get_session_maker().begin() as session:
            run = session.get(WorkbenchRun, run_id)
            if run.status == "environment_installing":
                if redis_client.get(scheduler.PREFIX + "stop:" + run_id):
                    run.status = "cancelled"
                else:
                    payload["continuation"] = {"calls": {payload["pending"]["tool_call_id"]: result}}
                    payload.pop("pending", None)
                    payload["attempt"] = payload.get("attempt", 0) + 1
                    run.payload, run.status, run.backend_run_id = json.dumps(payload), "queued", None
        # A second deferred request keeps the same user's queue closed until it is processed.
        with session_factory.create_session() as session:
            remaining = session.scalar(
                select(WorkbenchRun.id).where(
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                    WorkbenchRun.status.in_(["environment_update", "environment_installing"]),
                )
            )
        if remaining:
            update_environment.apply_async(args=(tenant_id, account_id), countdown=1)
        else:
            redis_client.delete(maintenance_key)
        scheduler.publish(tenant_id, account_id, run_id)
