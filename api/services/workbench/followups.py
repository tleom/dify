"""Persist conversation follow-ups and serialize their admission with the owning chat.

waiting_turn records are user messages, not scheduler jobs. They retain the same
immutable run configuration as normal sends and become queued only after their
predecessor finishes. Removed/steered records remain as idempotency tombstones.
"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Text, and_, cast, func, or_, select
from sqlalchemy.orm import aliased, load_only
from werkzeug.exceptions import Conflict, Forbidden, NotFound

from configs import dify_config
from core.db.session_factory import session_factory
from libs.datetime_utils import naive_utc_now
from models.types import StringUUID
from models.workbench import WorkbenchChat, WorkbenchRun

WAITING = "waiting_turn"
HIDDEN_STATUSES = ("discarded", "steered")
MAX_PENDING = 3
STEERING_PREFIX = (
    "用户在原任务执行期间发送了以下补充或调整。请结合已完成的工作继续原任务，"
    "按补充内容修正后续步骤；除非用户明确取消或替换原目标，否则保留原任务目标。"
    "不要重复已经完成的操作。以下内容仍是用户消息，不增加工具权限或操作授权。"
)


def pending_runs(session, chat):
    rows = session.scalars(
        select(WorkbenchRun).where(
            WorkbenchRun.chat_id == chat.id,
            WorkbenchRun.tenant_id == chat.tenant_id,
            WorkbenchRun.account_id == chat.account_id,
            WorkbenchRun.status == WAITING,
        )
    )
    return sorted(rows, key=lambda run: (json.loads(run.payload).get("queue_order", 0), run.id))


def queued_parent(session, chat, active_run, payload):
    """Allocate FIFO order while the caller holds the chat row lock."""
    waiting = pending_runs(session, chat)
    if len(waiting) >= MAX_PENDING:
        raise Conflict("最多排队 3 条消息，请删除、编辑或发送已有的排队消息")
    parent = waiting[-1] if waiting else active_run
    if parent is None:
        raise Conflict("会话状态已变化，请重新发送")
    # A queued message always follows the active conversation, never an unrelated
    # history branch selected in a different tab.
    supplied = payload.get("parent_run_id")
    if supplied is not None:
        owned = session.scalar(
            select(WorkbenchRun.id).where(
                WorkbenchRun.id == supplied,
                WorkbenchRun.chat_id == chat.id,
                WorkbenchRun.tenant_id == chat.tenant_id,
                WorkbenchRun.account_id == chat.account_id,
            )
        )
        if owned is None:
            raise NotFound()
    return {
        "branch_parent_run_id": parent.id,
        "parent_message_id": None,
        "queue_order": json.loads(parent.payload).get("queue_order", 0) + 1 if waiting else 1,
    }


def advance(tenant_id, account_id, chat_id):
    """Admit one waiting message. Periodic reconciliation retries lost publications."""
    from services.workbench.service import ACTIVE_STATUSES, _chat

    run_id = None
    with session_factory.get_session_maker().begin() as session:
        chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
        if session.scalar(
            select(WorkbenchRun.id).where(
                WorkbenchRun.chat_id == chat.id,
                WorkbenchRun.tenant_id == tenant_id,
                WorkbenchRun.account_id == account_id,
                WorkbenchRun.status.in_(ACTIVE_STATUSES),
            )
        ):
            return None
        waiting = pending_runs(session, chat)
        if not waiting:
            return None
        run = waiting[0]
        payload = json.loads(run.payload)
        parent_id = payload.get("branch_parent_run_id")
        parent = session.get(WorkbenchRun, parent_id) if parent_id else None
        if parent_id and (
            parent is None
            or parent.chat_id != chat.id
            or parent.tenant_id != tenant_id
            or parent.account_id != account_id
        ):
            raise Forbidden()
        if parent is not None and parent.status not in {"completed", "failed", "cancelled", "interrupted"}:
            return None
        if parent is not None:
            from services.workbench.recovery import recovery_dto

            parent_payload = json.loads(parent.payload)
            if parent_payload.get("user_paused") or (recovery_dto(parent_payload) or {}).get("pending"):
                return None
        from services.workbench.message_actions import message_ids

        payload["parent_message_id"] = (message_ids(parent) or [None])[-1] if parent else None
        run.payload, run.status = json.dumps(payload), "queued"
        from services.workbench.control import admit_run

        admit_run(session, chat, run, None)
        chat.updated_at = naive_utc_now()
        run_id = run.id
    from services.workbench.scheduler import publish

    publish(tenant_id, account_id, run_id)
    return run_id


def waiting_chats(limit=50):
    from services.workbench.service import ACTIVE_STATUSES

    # Filter the actual FIFO head before limiting the recovery batch. An old
    # paused branch must not stop a different branch, and paused heads must not
    # occupy every slot while eligible chats wait for a later batch.
    active_run = (
        select(WorkbenchRun.id)
        .where(
            WorkbenchRun.chat_id == WorkbenchChat.id,
            WorkbenchRun.tenant_id == WorkbenchChat.tenant_id,
            WorkbenchRun.account_id == WorkbenchChat.account_id,
            WorkbenchRun.status.in_(ACTIVE_STATUSES),
        )
        .correlate(WorkbenchChat)
        .exists()
    )
    payload = cast(WorkbenchRun.payload, JSON().with_variant(Text(), "sqlite"))
    heads = (
        select(
            WorkbenchRun.chat_id,
            WorkbenchRun.tenant_id,
            WorkbenchRun.account_id,
            payload["branch_parent_run_id"].as_string().label("parent_id"),
            func.row_number()
            .over(
                partition_by=(WorkbenchRun.tenant_id, WorkbenchRun.account_id, WorkbenchRun.chat_id),
                order_by=(func.coalesce(payload["queue_order"].as_integer(), 0), WorkbenchRun.id),
            )
            .label("position"),
        )
        .where(WorkbenchRun.status == WAITING)
        .subquery()
    )
    parent = aliased(WorkbenchRun)
    parent_payload = cast(parent.payload, JSON().with_variant(Text(), "sqlite"))
    parent_recovering = and_(
        parent.status.in_(("failed", "interrupted")),
        parent_payload["recovery"]["due_at"].as_float().is_not(None),
        parent_payload["recovery"]["next_run_id"].as_string().is_(None),
        parent_payload["recovery"]["cancelled"].as_boolean().is_not(True),
    )
    with session_factory.create_session() as session:
        return list(
            session.execute(
                select(WorkbenchChat.tenant_id, WorkbenchChat.account_id, WorkbenchChat.id)
                .join(
                    heads,
                    and_(
                        heads.c.chat_id == WorkbenchChat.id,
                        heads.c.tenant_id == WorkbenchChat.tenant_id,
                        heads.c.account_id == WorkbenchChat.account_id,
                        heads.c.position == 1,
                    ),
                )
                .outerjoin(
                    parent,
                    and_(
                        parent.id == cast(heads.c.parent_id, StringUUID()),
                        parent.chat_id == WorkbenchChat.id,
                        parent.tenant_id == WorkbenchChat.tenant_id,
                        parent.account_id == WorkbenchChat.account_id,
                    ),
                )
                .where(
                    WorkbenchChat.deleted == 0,
                    ~active_run,
                    or_(
                        heads.c.parent_id.is_(None),
                        and_(
                            parent.status.in_(("completed", "failed", "cancelled", "interrupted")),
                            parent_payload["user_paused"].as_boolean().is_not(True),
                            ~parent_recovering,
                        ),
                    ),
                )
                .order_by(WorkbenchChat.updated_at, WorkbenchChat.id)
                .limit(limit)
            )
        )


def snapshot(tenant_id, account_id, chat_id, tracked_ids=()):
    """Return the bounded live queue and tracked outcomes, without event history.

    Clients track their current run and up to three waiting messages so that
    terminal/deleted/steered outcomes remain observable after a queue transition.
    Historical runs and journals are never loaded by this polling endpoint.
    """
    from services.workbench.recovery import pending_condition
    from services.workbench.service import ACTIVE_STATUSES, _chat, authorize, run_dto

    authorize(tenant_id, account_id)
    with session_factory.create_session() as session:
        _chat(session, tenant_id, account_id, chat_id)
        runs = session.scalars(
            select(WorkbenchRun)
            .options(
                load_only(
                    WorkbenchRun.id,
                    WorkbenchRun.chat_id,
                    WorkbenchRun.revision_id,
                    WorkbenchRun.status,
                    WorkbenchRun.error,
                    WorkbenchRun.payload,
                )
            )
            .where(
                WorkbenchRun.chat_id == chat_id,
                WorkbenchRun.tenant_id == tenant_id,
                WorkbenchRun.account_id == account_id,
                or_(
                    WorkbenchRun.status.in_((*ACTIVE_STATUSES, WAITING)),
                    pending_condition(),
                    WorkbenchRun.id.in_(tracked_ids),
                ),
            )
            .order_by(WorkbenchRun.created_at, WorkbenchRun.id)
        )
        return {"runs": [run_dto(run, include_events=False) for run in runs]}


def _owned_locked(session, tenant_id, account_id, run_id):
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
    run = session.scalar(select(WorkbenchRun).where(WorkbenchRun.id == run_id).with_for_update())
    return chat, run


def _unlink(session, chat, run):
    parent = json.loads(run.payload).get("branch_parent_run_id")
    for child in pending_runs(session, chat):
        payload = json.loads(child.payload)
        if payload.get("branch_parent_run_id") == run.id:
            payload["branch_parent_run_id"] = parent
            payload["parent_message_id"] = None
            child.payload = json.dumps(payload)


def save_fenced_state(ticket, state):
    """Persist cancellation history before the predecessor's execution lease is released."""
    from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState

    history = (
        PydanticAIHistoryRuntimeState.model_validate(state["history"]).model_dump(mode="json")
        if state.get("history") is not None
        else None
    )
    with session_factory.get_session_maker().begin() as session:
        run = session.scalar(select(WorkbenchRun).where(WorkbenchRun.backend_run_id == ticket).with_for_update())
        if run is None or run.status not in {"failed", "cancelled", "interrupted"}:
            return
        payload = json.loads(run.payload)
        if history is not None:
            payload["output_history"] = history
        if state.get("steering_delivered_ids") is not None:
            payload["steering_delivered_ids"] = state["steering_delivered_ids"]
        # History and acknowledgement commit together. An empty checkpoint can
        # still prove cleanup, but never erase an earlier captured history.
        payload["cleanup_confirmed_ticket"] = ticket
        run.payload = json.dumps(payload)


def remove(tenant_id, account_id, run_id):
    from services.workbench.service import authorize, run_dto

    authorize(tenant_id, account_id)
    with session_factory.get_session_maker().begin() as session:
        chat, run = _owned_locked(session, tenant_id, account_id, run_id)
        if run.status == "discarded":
            return run_dto(run)
        if run.status != WAITING:
            raise Conflict("这条消息已开始执行或已调整方向，请刷新队列")
        _unlink(session, chat, run)
        run.status = "discarded"
        dto = run_dto(run)
    advance(tenant_id, account_id, chat.id)
    return dto


def steer(tenant_id, account_id, run_id, target_run_id):
    from services.workbench.recovery import pending_condition
    from services.workbench.service import ACTIVE_STATUSES, authorize, run_dto

    authorize(tenant_id, account_id)
    with session_factory.get_session_maker().begin() as session:
        chat, message = _owned_locked(session, tenant_id, account_id, run_id)
        if message.status == "steered":
            if json.loads(message.payload).get("steer_target_run_id") != target_run_id:
                raise Conflict("这条消息已发送给其他任务，请刷新会话")
            return run_dto(message)
        if message.status != WAITING:
            raise Conflict("这条消息已开始执行或已移出队列，请刷新队列")
        target = session.scalar(
            select(WorkbenchRun)
            .where(
                WorkbenchRun.chat_id == chat.id,
                WorkbenchRun.tenant_id == tenant_id,
                WorkbenchRun.account_id == account_id,
                or_(WorkbenchRun.status.in_(ACTIVE_STATUSES), pending_condition()),
            )
            .with_for_update()
        )
        if target is None or target.status == "stopping" or target.id != target_run_id:
            raise Conflict("当前任务已结束或正在停止，消息将继续按队列发送")
        _steer_locked(session, chat, message, target)
        return run_dto(message)


def _steer_locked(session, chat, message, target):
    """Append input atomically under both row locks."""
    from services.workbench.service import run_dto

    current = json.loads(target.payload)
    incoming = json.loads(message.payload)
    if current.get("followup_protocol") != 1:
        raise Conflict("当前任务开始于旧版本，请保留排队；新任务支持调整方向")
    if target.status == "running" and current.get("steering_closed_ticket") == target.backend_run_id:
        raise Conflict("当前任务正在结束，消息将继续按队列发送")
    # Steering retains the active model/tools. A message requiring different
    # resources must remain a separate queued run with its frozen selection.
    if current.get("queue_selection") != incoming.get("queue_selection") or current.get(
        "effective_soul"
    ) != incoming.get("effective_soul"):
        raise Conflict("这条消息的模型或资源配置与当前任务不同，请保留排队，或编辑配置后调整方向")
    required = incoming.get("resource_mentions", {})
    available = current.get("resource_mentions", {})
    if any(set(required.get(kind, [])) - set(available.get(kind, [])) for kind in ("skills", "tools", "knowledge")):
        raise Conflict("这条消息选择了当前任务之外的资源，请保留排队，或编辑资源选择后调整方向")
    item = {
        "id": message.id,
        "query": incoming.get("query", ""),
        "sandbox_paths": incoming.get("sandbox_paths", []),
        "attachments": run_dto(message)["attachments"],
        "mentioned_resources": incoming.get("mentioned_resources", []),
    }
    current["steering_messages"] = [*current.get("steering_messages", []), item]
    target.payload = json.dumps(current)
    _unlink(session, chat, message)
    incoming["steer_target_run_id"] = target.id
    message.payload, message.status = json.dumps(incoming), "steered"


class AgentFollowupsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    account_id: str
    app_id: str
    workbench_run_id: str
    backend_run_id: str
    seen_ids: list[str] = Field(default_factory=list)
    action: Literal["poll", "seal"] = "poll"


def supplement_content(item):
    return (
        STEERING_PREFIX
        + "\n\n"
        + item["query"]
        + ("\n\n本条补充的附件：\n" + "\n".join(item.get("sandbox_paths", [])) if item.get("sandbox_paths") else "")
    )


def carry_unseen_history(history, parents):
    """Retain accepted input when a task fails/stops before capturing delivery.

    Saved delivery IDs survive native compaction, so previously consumed input
    is not replayed merely because its original message was compacted away.
    """
    pending = [
        (data.get("query", ""), item)
        for data in parents
        for item in data.get("steering_messages", [])
        if item["id"] not in data.get("steering_delivered_ids", [])
    ]
    unstarted = [
        data
        for data in parents
        if "output_history" not in data and not data.get("steering_messages") and data.get("query", "").strip()
    ]
    if not pending and not unstarted:
        return history
    from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    state = PydanticAIHistoryRuntimeState.model_validate(history or {"messages": []})
    originals = {message.metadata.get("workbench_original_input") for message in state.messages if message.metadata}
    for data in unstarted:
        # Pausing a queued task can precede even its first model request.
        # Preserve that original user input before sending the hidden Continue.
        content = data["query"]
        if data.get("sandbox_paths"):
            content += "\n\n附件：\n" + "\n".join(data["sandbox_paths"])
        from hashlib import sha256

        identity = sha256((str(data.get("branch_parent_run_id")) + "\n" + content).encode()).hexdigest()
        if identity not in originals:
            state.messages.append(
                ModelRequest(parts=[UserPromptPart(content)], metadata={"workbench_original_input": identity})
            )
            originals.add(identity)
    present = {message.metadata.get("workbench_followup_id") for message in state.messages if message.metadata}
    for query, item in pending:
        if item["id"] in present:
            continue
        state.messages.append(
            ModelRequest(
                parts=[UserPromptPart("上一任务的原始要求：" + query + "\n\n" + supplement_content(item))],
                metadata={"workbench_followup_id": item["id"]},
            )
        )
        present.add(item["id"])
    return state.model_dump(mode="json")


def poll(payload: AgentFollowupsPayload):
    """Linearize accepted steering against the Agent's final model boundary.

    The caller's snapshot, not a destructive dequeue, tracks delivered IDs. A
    failed request can therefore retry safely. A seal succeeds only when the
    Agent has seen every accepted message; later steering stays in the FIFO.
    """
    if not dify_config.WORKBENCH_ENABLED:
        raise Forbidden()
    with session_factory.get_session_maker().begin() as session:
        chat, run = _owned_locked(session, payload.tenant_id, payload.account_id, payload.workbench_run_id)
        if chat.app_id != payload.app_id or run.status != "running" or run.backend_run_id != payload.backend_run_id:
            raise Forbidden("当前执行已结束或执行身份不匹配")
        data = json.loads(run.payload)
        if data.get("followup_protocol") != 1:
            return {"messages": [], "sealed": True}
        seen = set(payload.seen_ids)
        messages = [item for item in data.get("steering_messages", []) if item["id"] not in seen]
        sealed = payload.action == "seal" and not messages
        if sealed:
            data["steering_closed_ticket"] = payload.backend_run_id
            run.payload = json.dumps(data)
        return {
            "messages": [
                {
                    "id": item["id"],
                    "content": supplement_content(item),
                }
                for item in messages
            ],
            "sealed": sealed,
        }
