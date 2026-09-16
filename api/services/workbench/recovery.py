"""Persist bounded continuation and untouched human-input deadlines with each run.

The database owns intent and idempotency; Celery only wakes that intent. All
changes serialize with manual sends, resume, deletion and pause on chat -> run
locks. A failed executor is fenced before another execution may be queued.
"""

from __future__ import annotations

import json
import time
from uuid import uuid4

from sqlalchemy import JSON, Text, and_, cast, select, update
from werkzeug.exceptions import Conflict, NotFound

from core.db.session_factory import session_factory
from libs.datetime_utils import naive_utc_now
from models.workbench import WorkbenchChat, WorkbenchRun
from services.workbench import scheduler
from services.workbench.event_log import append_locked, notify

MAX_AUTO_CONTINUATIONS = 3
INPUT_TIMEOUT_SECONDS = 60
CONTINUATION_DELAY_SECONDS = 2


def save_fenced_history(ticket, state):
    """Recover a stopped executor's history if its API consumer died first."""
    from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState

    history = PydanticAIHistoryRuntimeState.model_validate(state).model_dump(mode="json")
    with session_factory.get_session_maker().begin() as session:
        run = session.scalar(select(WorkbenchRun).where(WorkbenchRun.backend_run_id == ticket).with_for_update())
        if run is None or run.status not in {"failed", "cancelled", "interrupted"}:
            return
        payload = json.loads(run.payload)
        payload["output_history"] = history
        run.payload = json.dumps(payload)


def payload_json():
    # SQLite stores JSON as text; CAST AS JSON there coerces it to the number 0.
    return cast(WorkbenchRun.payload, JSON().with_variant(Text(), "sqlite"))


def cleanup_pending_condition():
    """A terminal SQL status is not proof that its remote executor has stopped.

    The ticket is persisted before dispatch. Missing confirmation on an older
    failed/cancelled run is conservative: fence it once before admitting work.
    Successful terminal streams confirm their ticket in the status transaction.
    """
    return and_(
        WorkbenchRun.status.in_(("failed", "cancelled", "interrupted")),
        WorkbenchRun.backend_run_id.is_not(None),
        payload_json()["cleanup_confirmed_ticket"]
        .as_string()
        .is_distinct_from(cast(WorkbenchRun.backend_run_id, Text)),
    )


def pending_condition():
    payload = payload_json()
    return and_(
        WorkbenchRun.status.in_(["failed", "interrupted"]),
        payload["recovery"]["due_at"].as_float().is_not(None),
        payload["recovery"]["next_run_id"].as_string().is_(None),
        payload["recovery"]["cancelled"].as_boolean().is_not(True),
    )


def recovery_dto(payload):
    value = payload.get("recovery")
    if not isinstance(value, dict):
        return None
    return {
        "attempt": value.get("attempt", 0),
        "limit": MAX_AUTO_CONTINUATIONS,
        "pending": bool(value.get("due_at") and not value.get("next_run_id") and not value.get("cancelled")),
        "next_run_id": value.get("next_run_id"),
    }


def input_dto(payload):
    value = payload.get("human_input")
    return {**value, "server_now": time.time()} if isinstance(value, dict) else None


def mark_failure(run):
    """Called under the run lock when this attempt becomes failed/interrupted."""
    payload = json.loads(run.payload)
    recovery = payload.get("recovery")
    if (
        run.status not in {"failed", "interrupted"}
        or payload.get("control", {}).get("kind") == "compact"
        or not isinstance(recovery, dict)
        or recovery.get("cancelled")
        or recovery.get("next_run_id")
        or recovery.get("attempt", 0) >= MAX_AUTO_CONTINUATIONS
    ):
        return False
    recovery.setdefault("due_at", time.time() + CONTINUATION_DELAY_SECONDS)
    run.payload = json.dumps(payload)
    return True


def mark_input_wait(run):
    """Start a fresh deadline only after the question becomes visible."""
    payload = json.loads(run.payload)
    pending = payload.get("pending", {})
    if run.status != "waiting_input" or pending.get("tool_name") != "ask_human":
        return
    payload["human_input"] = {
        "request_id": str(uuid4()),
        "tool_call_id": pending["tool_call_id"],
        "deadline_at": time.time() + INPUT_TIMEOUT_SECONDS,
        "interacted": False,
    }
    run.payload = json.dumps(payload)


def locked_run(session, tenant_id, account_id, run_id):
    from services.workbench.service import _chat

    chat_id = session.scalar(
        select(WorkbenchRun.chat_id).where(
            WorkbenchRun.id == run_id,
            WorkbenchRun.tenant_id == tenant_id,
            WorkbenchRun.account_id == account_id,
        )
    )
    if chat_id is None:
        raise NotFound()
    chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
    run = session.scalar(
        select(WorkbenchRun)
        .where(
            WorkbenchRun.id == run_id,
            WorkbenchRun.tenant_id == tenant_id,
            WorkbenchRun.account_id == account_id,
        )
        .with_for_update()
    )
    if run is None:
        raise NotFound()
    return chat, run


def interact(tenant_id, account_id, run_id, request_id):
    from services.workbench.service import authorize, run_dto

    authorize(tenant_id, account_id)
    with session_factory.get_session_maker().begin() as session:
        _, run = locked_run(session, tenant_id, account_id, run_id)
        payload = json.loads(run.payload)
        state = payload.get("human_input", {})
        if run.status != "waiting_input" or state.get("request_id") != request_id:
            raise Conflict("此补充信息请求已结束，请刷新任务状态")
        state["interacted"] = True
        state["deadline_at"] = None
        run.payload = json.dumps(payload)
        return run_dto(run)


def expire_input(run_id, *, owner=None, request_id=None, manual=False):
    from dify_agent.layers.ask_human.schema import AskHumanToolResult

    from services.workbench.service import authorize

    if manual and (owner is None or request_id is None):
        raise ValueError("Manual skip requires the authenticated owner and request ID")
    with session_factory.create_session() as session:
        source = session.get(WorkbenchRun, run_id)
        if source is None:
            return False
        tenant_id, account_id = source.tenant_id, source.account_id
        if owner is not None and owner != (tenant_id, account_id):
            raise NotFound()
    authorize(tenant_id, account_id)
    cursor = None
    with session_factory.get_session_maker().begin() as session:
        _, run = locked_run(session, tenant_id, account_id, run_id)
        payload = json.loads(run.payload)
        state, pending = payload.get("human_input", {}), payload.get("pending", {})
        deadline = state.get("deadline_at")
        expected_id = state.get("request_id") or (pending.get("tool_call_id") if manual else None)
        if (
            run.status != "waiting_input"
            or pending.get("tool_name") != "ask_human"
            or (request_id is not None and request_id != expected_id)
            or (
                not manual
                and (
                    state.get("interacted")
                    or not deadline
                    or deadline > time.time()
                    or state.get("tool_call_id") != pending.get("tool_call_id")
                )
            )
        ):
            return False
        reason = (
            "用户选择跳过此补充信息问题，未提交答案，请继续原任务。"
            if manual
            else "用户在 60 秒内未操作补充信息框，未提供答案。"
        )
        result = AskHumanToolResult(
            status="cancelled" if manual else "timeout",
            message=(
                reason + "请依据已有上下文自行决定可合理推断的细节，"
                "说明必要假设并继续原任务。未回复不代表确认了事实或授予额外权限，不要编造关键事实。"
            ),
        )
        payload["continuation"] = {"calls": {pending["tool_call_id"]: result.model_dump(mode="json")}}
        from services.workbench.human_input import remember

        remember(run, payload, pending, result.model_dump(mode="json"))
        payload["last_input_timeout"] = expected_id
        payload.pop("pending", None)
        payload.pop("human_input", None)
        payload.pop("submitted_input", None)
        if manual:
            payload["recovery"] = {"attempt": 0}
        else:
            payload.setdefault("recovery", {"attempt": 0})
        payload["attempt"] = payload.get("attempt", 0) + 1
        run.payload, run.status, run.backend_run_id = json.dumps(payload), "queued", None
        cursor = append_locked(
            session,
            run,
            {
                "event": "workbench_status",
                "status": "queued",
                "input_timed_out": not manual,
                "input_skipped": manual,
            },
        )
    if cursor is not None:
        notify(run_id, cursor)
    scheduler.publish(tenant_id, account_id, run_id)
    return True


def continue_failed(run_id):
    """Queue one model continuation; do not replay any individual tool call."""
    from services.workbench.followups import HIDDEN_STATUSES, WAITING, pending_runs
    from services.workbench.message_actions import message_ids
    from services.workbench.service import authorize
    from tasks.workbench_tasks import fence_remote

    with session_factory.create_session() as session:
        source = session.get(WorkbenchRun, run_id)
        if source is None:
            return None
        source_payload = json.loads(source.payload)
        recovery = source_payload.get("recovery", {})
        state = recovery_dto(source_payload)
        if not state or not state["pending"]:
            return recovery.get("next_run_id")
        if source.status not in {"failed", "interrupted"} or recovery["due_at"] > time.time():
            return None
        tenant_id, account_id, ticket = source.tenant_id, source.account_id, source.backend_run_id
    authorize(tenant_id, account_id)
    if not fence_remote(ticket):
        return None
    scheduler.release(f"{tenant_id}:{account_id}", run_id)
    with session_factory.get_session_maker().begin() as session:
        chat, source = locked_run(session, tenant_id, account_id, run_id)
        payload = json.loads(source.payload)
        recovery = payload.get("recovery", {})
        if recovery.get("next_run_id"):
            return recovery["next_run_id"]
        if (
            source.status not in {"failed", "interrupted"}
            or recovery.get("cancelled")
            or recovery.get("attempt", 0) >= MAX_AUTO_CONTINUATIONS
        ):
            return None
        turns = list(
            session.execute(
                select(
                    WorkbenchRun.id,
                    WorkbenchRun.created_at,
                    payload_json()["branch_parent_run_id"].as_string().label("parent_id"),
                ).where(
                    WorkbenchRun.chat_id == chat.id,
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                    WorkbenchRun.status.not_in((WAITING, *HIDDEN_STATUSES)),
                )
            )
        )
        parents = {turn.id: turn.parent_id for turn in turns}
        ancestors = set()
        parent_id = parents.get(source.id)
        while parent_id and parent_id not in ancestors:
            ancestors.add(parent_id)
            parent_id = parents.get(parent_id)
        superseded = any(
            turn.parent_id == source.id
            or (turn.id not in ancestors and (turn.created_at, turn.id) > (source.created_at, source.id))
            for turn in turns
        )
        if superseded:
            # A started manual message or another continuation has advanced this
            # chat. Waiting/removed follow-ups do not supersede the current task.
            # A queued message can predate its resumed ancestors, so their
            # creation timestamps must never cancel its own recovery budget.
            recovery["cancelled"] = True
            source.payload = json.dumps(payload)
            return None
        new_id = str(uuid4())
        next_payload = {
            key: payload[key]
            for key in (
                "effective_soul",
                "version",
                "template_snapshot_id",
                "activity_protocol",
                "inputs",
                "resource_mentions",
                "mentioned_resources",
                "mention_prompt",
                "sandbox_paths",
                "image_files",
                "followup_protocol",
                "queue_selection",
                "queue_files",
                "control",
            )
            if key in payload
        }
        next_payload.update(
            query="继续",
            is_continuation=True,
            attempt=0,
            branch_parent_run_id=source.id,
            parent_message_id=(message_ids(source) or [None])[-1],
            recovery={
                "attempt": recovery.get("attempt", 0) + 1,
                "root_run_id": recovery.get("root_run_id", source.id),
                "goal": recovery.get("goal", payload.get("query", "")),
                "previous_error": source.error,
            },
        )
        run = WorkbenchRun(
            id=new_id,
            tenant_id=tenant_id,
            account_id=account_id,
            chat_id=chat.id,
            revision_id=source.revision_id,
            request_key=f"auto-continue:{source.id}",
            payload=json.dumps(next_payload),
            status="queued",
            event_log="[]",
        )
        session.add(run)
        recovery["next_run_id"] = new_id
        recovery.pop("due_at", None)
        source.payload = json.dumps(payload)
        waiting = pending_runs(session, chat)
        if waiting and json.loads(waiting[0].payload).get("branch_parent_run_id") == source.id:
            head = waiting[0]
            queued_payload = json.loads(head.payload)
            queued_payload["branch_parent_run_id"] = new_id
            queued_payload["parent_message_id"] = None
            head.payload = json.dumps(queued_payload)
        chat.updated_at = naive_utc_now()
    scheduler.publish(tenant_id, account_id, new_id)
    return new_id


def cancel_chain(tenant_id, account_id, run_id):
    """Pause also wins if the failed attempt has already queued its successor."""
    from services.workbench.event_log import uses_journal
    from services.workbench.followups import HIDDEN_STATUSES, WAITING

    targets, cursors = [], []
    with session_factory.get_session_maker().begin() as session:
        chat, run = locked_run(session, tenant_id, account_id, run_id)
        if run.status in (WAITING, *HIDDEN_STATUSES):
            raise Conflict("这条消息尚未独立执行，请使用队列移除操作")
        from services.workbench.control import pause_goal

        pause_goal(session, chat)
        for _ in range(MAX_AUTO_CONTINUATIONS + 1):
            payload = json.loads(run.payload)
            state = payload.setdefault("recovery", {"attempt": 0})
            state["cancelled"] = True
            state.pop("due_at", None)
            payload.pop("human_input", None)
            if run.status != "completed":
                payload["user_paused"] = True
                run.status, run.error = "cancelled", None
            run.payload = json.dumps(payload)
            if run.status == "cancelled":
                targets.append(run.id)
                if uses_journal(run):
                    cursors.append(
                        (
                            run.id,
                            append_locked(
                                session,
                                run,
                                {
                                    "event": "workbench_end",
                                    "status": "cancelled",
                                    "error": None,
                                    "user_paused": True,
                                    "recovery": recovery_dto(payload),
                                },
                            ),
                        )
                    )
            next_id = state.get("next_run_id")
            if not next_id:
                break
            child = session.scalar(
                select(WorkbenchRun)
                .where(
                    WorkbenchRun.id == next_id,
                    WorkbenchRun.chat_id == chat.id,
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                )
                .with_for_update()
            )
            if child is None or child.request_key != f"auto-continue:{run.id}":
                break
            run = child
    for target, cursor in cursors:
        if cursor is not None:
            notify(target, cursor)
    return targets


def due_runs():
    """Rotate bounded timer scans so blocked owners cannot starve later tasks."""
    payload = payload_json()
    with session_factory.get_session_maker().begin() as session:
        failed = list(
            session.scalars(
                select(WorkbenchRun.id)
                .join(
                    WorkbenchChat,
                    WorkbenchChat.id == WorkbenchRun.chat_id,
                )
                .where(
                    WorkbenchChat.deleted == 0,
                    pending_condition(),
                    payload["recovery"]["due_at"].as_float() <= time.time(),
                )
                .order_by(WorkbenchRun.updated_at)
                .limit(50)
            )
        )
        waiting = list(
            session.scalars(
                select(WorkbenchRun.id)
                .join(
                    WorkbenchChat,
                    WorkbenchChat.id == WorkbenchRun.chat_id,
                )
                .where(
                    WorkbenchChat.deleted == 0,
                    WorkbenchRun.status == "waiting_input",
                    payload["human_input"]["deadline_at"].as_float() <= time.time(),
                    payload["human_input"]["interacted"].as_boolean().is_(False),
                )
                .order_by(WorkbenchRun.updated_at)
                .limit(50)
            )
        )
        # Advance only scan bookkeeping, never execution state or intent. A
        # revoked account, unavailable executor or failed dispatch remains due
        # but moves behind tasks which have not yet received a recovery attempt.
        if failed or waiting:
            session.execute(
                update(WorkbenchRun).where(WorkbenchRun.id.in_([*failed, *waiting])).values(updated_at=naive_utc_now())
            )
    return failed, waiting
