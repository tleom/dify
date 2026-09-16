"""Durable workbench queue, isolated execution, and deferred environment maintenance."""

import json
import logging
import threading
import time
from collections.abc import Mapping
from typing import cast

import httpx
from celery import shared_task
from flask import Flask, current_app
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from werkzeug.local import LocalProxy

from configs import dify_config
from core.app.apps.agent_app.app_generator import AgentAppGenerator
from core.app.entities.app_invoke_entities import InvokeFrom
from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from models import Account
from models.model import App, AppMode
from models.workbench import WorkbenchChat, WorkbenchRun
from services.app_task_service import AppTaskService
from services.workbench import maintenance, recovery, scheduler
from services.workbench import runtime as workbench_runtime
from services.workbench.event_log import append_event, append_locked, notify, uses_journal
from services.workbench.service import authorize

logger = logging.getLogger(__name__)


def event(run_id, payload):
    return append_event(run_id, payload, expected_backend_run_id=payload.get("backend_run_id"))


def stop_native(run_id, account_id):
    # This notification is best effort; the durable remote fence below remains
    # authoritative even when Redis or the old API task is no longer available.
    try:
        task_id = redis_client.get(scheduler.PREFIX + "task:" + run_id)
        if task_id:
            task_id = task_id.decode() if isinstance(task_id, bytes) else task_id
            AppTaskService.stop_task(task_id, InvokeFrom.EXPLORE, account_id, AppMode.AGENT)
    except Exception:
        logger.warning("Workbench stop notification failed; remote fencing will continue: %s", run_id, exc_info=True)


def fence_remote(ticket):
    """Do not release capacity until cancellation has reached a terminal remote state."""
    from dify_agent.protocol.schemas import FenceRunResponse

    if not ticket:
        return True
    endpoint = dify_config.AGENT_BACKEND_BASE_URL
    token = dify_config.AGENT_BACKEND_API_TOKEN
    if not endpoint or not token:
        raise RuntimeError("Agent backend endpoint and token are required to fence a Workbench run")
    headers = {"Authorization": "Bearer " + token}
    # The executor's sandbox recovery manager permits 90 seconds of cleanup.
    # Leave time for its response and checkpoint while keeping connection setup bounded.
    timeout = httpx.Timeout(10.0, read=120.0)
    with httpx.Client(base_url=endpoint, headers=headers, timeout=timeout, trust_env=False) as client:
        result = client.post(f"/runs/{ticket}/fence", json={})
        result.raise_for_status()
        response = FenceRunResponse.model_validate(result.json())
        if response.run_id != ticket:
            raise ValueError("Remote cleanup response does not match the execution ticket")
        state = response.model_dump(mode="json")
        if state["status"] != "running":
            from services.workbench.followups import save_fenced_state

            save_fenced_state(ticket, state)
        # running means the remote owner still needs to finish cleanup.
        return state["status"] != "running"


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
        execute.apply_async(args=tuple(claimed), task_id="workbench-" + claimed[1])


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
        # Redis can restart with an empty lease set while the database still
        # records running tasks. Recover those too, after checking the live lease.
        orphaned = list(
            session.scalars(
                select(WorkbenchRun.id)
                .where(
                    WorkbenchRun.status == "running",
                    WorkbenchRun.id.not_in(active),
                )
                .limit(200)
            )
        )
        # Redis loss can also hide executors whose cancellation already
        # committed. Rotate unavailable tickets so they cannot starve the scan.
        unconfirmed = list(
            session.scalars(
                select(WorkbenchRun.id)
                .where(recovery.cleanup_pending_condition())
                .order_by(
                    func.coalesce(recovery.payload_json()["cleanup_checked_at"].as_float(), 0),
                    WorkbenchRun.id,
                )
                .limit(200)
            )
        )
    for run_id in dict.fromkeys([*expired, *terminal, *orphaned, *unconfirmed]):
        end_event = None
        with session_factory.create_session() as session:
            observed = session.execute(
                select(WorkbenchRun.status, WorkbenchRun.backend_run_id, WorkbenchRun.updated_at).where(
                    WorkbenchRun.id == run_id
                )
            ).one_or_none()
        if observed is None:
            continue
        if observed.status in ("running", "queued"):
            # A slow Redis reply must not hold a run lock or turn a lease that
            # was fresh when queried into an expired observation. Heartbeats
            # cannot revive a lease that had already expired before this lookup.
            lease_checked_at = time.time()
            lease = redis_client.zscore(scheduler.PREFIX + "active", run_id)
            if lease is not None and lease >= lease_checked_at:
                continue
        with session_factory.get_session_maker().begin() as session:
            # Skip busy or changed rows; a later scan will observe their current
            # execution. In particular, never fence a replacement ticket using
            # a lease observation made for its predecessor.
            run = session.scalar(
                select(WorkbenchRun)
                .where(
                    WorkbenchRun.id == run_id,
                    WorkbenchRun.status == observed.status,
                    WorkbenchRun.backend_run_id.is_not_distinct_from(observed.backend_run_id),
                )
                .with_for_update(skip_locked=True)
            )
            if run is None or run.updated_at != observed.updated_at:
                continue
            owner = f"{run.tenant_id}:{run.account_id}"
            ticket = run.backend_run_id
            if run.status in ("running", "queued"):
                run.status = "interrupted"
                run.error = "执行进程失联，正在确认旧执行已结束并保留进度。"
                recovery.mark_failure(run)
                if uses_journal(run):
                    end_event = append_locked(
                        session,
                        run,
                        {
                            "event": "workbench_end",
                            "status": run.status,
                            "error": run.error,
                            "recovery": recovery.recovery_dto(json.loads(run.payload)),
                        },
                    )
            account_id = run.account_id
            if ticket:
                payload = json.loads(run.payload)
                payload["cleanup_checked_at"] = time.time()
                run.payload = json.dumps(payload)
        if end_event is not None:
            notify(run_id, end_event)
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
    failed, waiting = recovery.due_runs()
    for run_id in failed:
        recover_run.delay(run_id)
    for run_id in waiting:
        expire_human_input.delay(run_id)
    from services.workbench.followups import waiting_chats

    for owner in waiting_chats():
        advance_followups.delay(*owner)
    from services.workbench.control import active_goal_chats

    for owner in active_goal_chats():
        advance_followups.delay(*owner)
    dispatch.delay()


@shared_task(queue="workbench_control")
def recover_run(run_id):
    if dify_config.WORKBENCH_ENABLED:
        return recovery.continue_failed(run_id)


@shared_task(queue="workbench_control")
def expire_human_input(run_id):
    if dify_config.WORKBENCH_ENABLED:
        return recovery.expire_input(run_id)


@shared_task(queue="workbench_control")
def advance_followups(tenant_id, account_id, chat_id):
    from services.workbench.control import drive_goal, settle
    from services.workbench.followups import advance

    settle(tenant_id, account_id, chat_id)
    if advance(tenant_id, account_id, chat_id) is None:
        drive_goal(tenant_id, account_id, chat_id)


@shared_task(queue="workbench", acks_late=False, reject_on_worker_lost=False)
def execute(owner, run_id):
    tenant_id, account_id = owner.split(":", 1)
    # A newly submitted turn may queue immediately while its predecessor cleans up.
    blocked = False
    with session_factory.create_session() as session:
        current = session.get(WorkbenchRun, run_id)
        if (
            current is not None
            and current.tenant_id == tenant_id
            and current.account_id == account_id
            and current.status == "queued"
        ):
            previous_ids = set(
                session.scalars(
                    select(WorkbenchRun.id).where(
                        WorkbenchRun.chat_id == current.chat_id,
                        WorkbenchRun.tenant_id == tenant_id,
                        WorkbenchRun.account_id == account_id,
                        WorkbenchRun.created_at < current.created_at,
                    )
                )
            )
            # Enqueue timestamps may share one database clock tick. The explicit
            # follow-up parent must also finish remote cleanup before admission.
            if parent_id := json.loads(current.payload).get("branch_parent_run_id"):
                previous_ids.add(parent_id)
            unconfirmed = session.scalar(
                select(WorkbenchRun.id)
                .where(
                    WorkbenchRun.chat_id == current.chat_id,
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                    WorkbenchRun.id != current.id,
                    recovery.cleanup_pending_condition(),
                )
                .limit(1)
            )
            blocked = unconfirmed is not None or any(
                redis_client.zscore(scheduler.PREFIX + "active", prior) is not None for prior in previous_ids
            )
    if blocked:
        if scheduler.heartbeat(owner, run_id):
            execute.apply_async(args=(owner, run_id), countdown=1)
        return
    done = threading.Event()
    claimed = False
    completed_stream = False
    events = []
    journal = False
    status, error = "completed", None
    app = cast(LocalProxy[Flask], current_app)._get_current_object()
    try:
        with session_factory.get_session_maker().begin() as session:
            claimed = bool(
                cast(
                    CursorResult,
                    session.execute(
                        update(WorkbenchRun)
                        .where(
                            WorkbenchRun.id == run_id,
                            WorkbenchRun.tenant_id == tenant_id,
                            WorkbenchRun.account_id == account_id,
                            WorkbenchRun.status == "queued",
                        )
                        .values(status="running")
                    ),
                ).rowcount
            )
            if not claimed:
                return
            run = session.get(WorkbenchRun, run_id)
            if run is None:
                raise ValueError("Workbench run is unavailable")
            chat = session.get(WorkbenchChat, run.chat_id)
            if chat is None:
                raise ValueError("Workbench conversation is unavailable")
            payload, conversation_id, app_id = json.loads(run.payload), chat.conversation_id, chat.app_id
            events = json.loads(run.event_log)
            journal = uses_journal(run, payload)
        authorize(tenant_id, account_id)
        if not scheduler.heartbeat(owner, run_id):
            raise RuntimeError("Admission lease expired before execution")
        event(run_id, {"event": "workbench_status", "status": "running"})

        def renew():
            last_success = time.monotonic()
            while not done.wait(5):
                try:
                    leased = scheduler.heartbeat(owner, run_id)
                    if leased:
                        last_success = time.monotonic()
                    if not leased or redis_client.get(scheduler.PREFIX + "stop:" + run_id):
                        with app.app_context():
                            stop_native(run_id, account_id)
                except Exception:
                    logger.exception("Workbench heartbeat failed")
                    # A short Redis outage is not evidence that the executor
                    # failed. Stop only once the last confirmed 90 s lease ends.
                    if time.monotonic() - last_success >= 90:
                        with app.app_context():
                            try:
                                stop_native(run_id, account_id)
                            except Exception:
                                logger.warning("Native stop unavailable; ticket fencing will retry", exc_info=True)

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
                result = AgentAppGenerator(workbench=workbench_runtime).generate(
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
                terminal_received = False
                for chunk in result:
                    if isinstance(chunk, Mapping):
                        frames = [dict(chunk)]
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
                        if item.get("event") in {"message_end", "error"}:
                            terminal_received = True
                        if item.get("event") == "workbench_context" and isinstance(item.get("data"), dict):
                            context_data = item.pop("data")
                            item.update(context_data)
                        item["workbench_run_id"] = run_id
                        if not journal:
                            events.append(item)
                        cursor = event(run_id, item)
                        if cursor is not None:
                            item["_id"] = cursor.decode() if isinstance(cursor, bytes) else str(cursor)
                        if item.get("event") == "error":
                            status, error = "failed", item.get("message", "Agent 执行失败")
                            break
                    # The terminal error is authoritative even if the upstream
                    # SSE iterator keeps sending heartbeats instead of closing.
                    # Finish this attempt and fence its ticket before recovery.
                    if status == "failed":
                        break
                completed_stream = terminal_received
                if not terminal_received:
                    status, error = "failed", "执行事件流提前结束，已保留进度，正在恢复任务。"
    except Exception:
        logger.exception("Workbench run failed: %s", run_id)
        status, error = "failed", "Agent 执行失败，请查看管理员日志后重试"
    finally:
        done.set()
        if claimed:
            pause_event = None
            end_event = None
            should_recover = False
            input_deadline = None
            with session_factory.get_session_maker().begin() as session:
                run = session.scalar(select(WorkbenchRun).where(WorkbenchRun.id == run_id).with_for_update())
                if run is None:
                    raise ValueError("Claimed Workbench run is unavailable during cleanup")
                ticket = run.backend_run_id
                if run.status == "cancelled":
                    status, error = "cancelled", None
                    run.status = status
                elif run.status in ("environment_update", "waiting_input"):
                    status = run.status
                elif run.status == "running":
                    pause_event = workbench_runtime.complete_pause(
                        session,
                        run,
                        completed_stream=completed_stream and status == "completed",
                    )
                    if pause_event is not None:
                        status = run.status
                    else:
                        run.status, run.error = status, error
                else:
                    status, error = run.status, run.error
                should_recover = recovery.mark_failure(run)
                input_deadline = json.loads(run.payload).get("human_input", {}).get("deadline_at")
                if not journal:
                    run.event_log = json.dumps(events)
                else:
                    end_event = append_locked(
                        session,
                        run,
                        {
                            "event": "workbench_end",
                            "status": status,
                            "error": error,
                            "recovery": recovery.recovery_dto(json.loads(run.payload)),
                        },
                    )
                safe_to_release = completed_stream and status in ("completed", "environment_update", "waiting_input")
                if safe_to_release and ticket:
                    payload = json.loads(run.payload)
                    payload["cleanup_confirmed_ticket"] = ticket
                    run.payload = json.dumps(payload)
            if pause_event is not None:
                notify(run_id, pause_event)
            if end_event is not None:
                notify(run_id, end_event)
            if not safe_to_release:
                try:
                    safe_to_release = fence_remote(ticket)
                except Exception:
                    logger.warning("Could not confirm remote cleanup for %s", run_id, exc_info=True)
            if safe_to_release:
                scheduler.release(owner, run_id)
            if not journal:
                event(
                    run_id,
                    {
                        "event": "workbench_end",
                        "status": status,
                        "error": error,
                        "recovery": recovery.recovery_dto(json.loads(run.payload)),
                    },
                )
            if status == "environment_update":
                update_environment.delay(tenant_id, account_id)
            elif status == "waiting_input" and input_deadline:
                expire_human_input.apply_async(args=(run_id,), countdown=max(0, input_deadline - time.time()))
            if should_recover:
                recover_run.apply_async(args=(run_id,), countdown=recovery.CONTINUATION_DELAY_SECONDS)
            if not should_recover and status in ("completed", "failed", "cancelled", "interrupted"):
                advance_followups.delay(tenant_id, account_id, run.chat_id)
            dispatch.delay()
        else:
            # A late delivery for a cancelled/finished task must not leak its reservation.
            with session_factory.create_session() as session:
                run = session.get(WorkbenchRun, run_id)
                if run is not None and run.status not in ("queued", "running"):
                    scheduler.release(owner, run_id)


@shared_task(queue="workbench_environment", acks_late=False)
def update_environment(tenant_id, account_id):
    from services.workbench import control
    from services.workbench.files import ensure_workspace, manager
    from services.workbench.recovery import locked_run

    owner = f"{tenant_id}:{account_id}"
    maintenance_key = scheduler.PREFIX + "maintenance:" + owner
    with redis_client.lock(scheduler.PREFIX + "environment-lock:" + owner, timeout=1200, blocking_timeout=1):
        if redis_client.zcard(scheduler.PREFIX + "active:" + owner):
            return
        pending_run = None
        plan_active = False
        with session_factory.get_session_maker().begin() as session:
            run_id = session.scalar(
                select(WorkbenchRun.id)
                .where(
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                    WorkbenchRun.status.in_(["environment_update", "environment_installing"]),
                )
                .order_by(WorkbenchRun.created_at)
                .limit(1)
            )
            if run_id is not None:
                chat, run = locked_run(session, tenant_id, account_id, run_id)
                if run.status in {"environment_update", "environment_installing"}:
                    pending_run = run.id, run.status, json.loads(run.payload)
                    plan_active = control.load(session, chat).plan.active
                    run.status = "environment_installing"
        if pending_run is None:
            try:
                if not maintenance.cancelled_installations_finished(tenant_id, account_id, manager):
                    return
            except Exception:
                logger.warning("Cancelled installer cleanup will be retried: %s", owner, exc_info=True)
                return
            redis_client.delete(maintenance_key)
            dispatch.delay()
            return
        run_id, previous_status, payload = pending_run
        result = None
        try:
            if previous_status != "environment_installing":
                authorize(tenant_id, account_id)
            request_id = f"{run_id}:{payload.get('attempt', 0)}"
            if plan_active and previous_status != "environment_installing":
                result = {"status": "failed", "error": "计划尚未批准，未更新共享运行环境，请先提交完整方案供用户审阅"}
            else:
                workspace = ensure_workspace(tenant_id, account_id)
                if previous_status == "environment_installing":
                    result = manager(workspace, "environment-status", {"request_id": request_id})
                    if result["status"] == "installing":
                        return
                else:
                    # Startup can wait. Recheck the ticket and the user's latest
                    # mode before dispatch; never hold a DB write lock during IO.
                    with session_factory.get_session_maker().begin() as session:
                        chat, run = locked_run(session, tenant_id, account_id, run_id)
                        current_payload = json.loads(run.payload)
                        if run.status != "environment_installing" or current_payload.get("attempt", 0) != payload.get(
                            "attempt", 0
                        ):
                            return
                        if control.load(session, chat).plan.active:
                            result = {
                                "status": "failed",
                                "error": "计划尚未批准，未更新共享运行环境，请先提交完整方案供用户审阅",
                            }
                    if result is None:
                        event(run_id, {"event": "workbench_status", "status": "environment_installing"})
                        result = manager(
                            workspace,
                            "environment",
                            {**payload["pending"]["args"], "request_id": request_id},
                            timeout=950,
                        )
                        if result["status"] == "installing":
                            return
        except Exception:
            logger.exception("Workbench environment update failed")
            # A lost response is not evidence that the installer stopped. Reconcile by request ID.
            return
        end_event = None
        with session_factory.get_session_maker().begin() as session:
            run = session.scalar(select(WorkbenchRun).where(WorkbenchRun.id == run_id).with_for_update())
            if run is None:
                raise ValueError("Workbench run is unavailable after environment update")
            current_payload = json.loads(run.payload)
            if current_payload.get("attempt", 0) != payload.get("attempt", 0) or current_payload.get("pending", {}).get(
                "tool_call_id"
            ) != payload.get("pending", {}).get("tool_call_id"):
                return
            payload = current_payload
            if run.status == "environment_installing":
                if redis_client.get(scheduler.PREFIX + "stop:" + run_id):
                    run.status = "cancelled"
                    if uses_journal(run):
                        end_event = append_locked(
                            session, run, {"event": "workbench_end", "status": "cancelled", "error": None}
                        )
                else:
                    payload["continuation"] = {"calls": {payload["pending"]["tool_call_id"]: result}}
                    payload.pop("pending", None)
                    payload["attempt"] = payload.get("attempt", 0) + 1
                    run.payload, run.status, run.backend_run_id = json.dumps(payload), "queued", None
        if end_event is not None:
            notify(run_id, end_event)
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
