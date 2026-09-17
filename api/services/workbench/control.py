"""Conversation-locked collaboration state and exact-execution Agent control.

The chat row is the serialization point shared with send/stop/follow-up.
Network calls and job publication happen after committing. Commands have durable
idempotency records; periodic goal admission recovers a lost publication.
"""

from __future__ import annotations

import json
from datetime import UTC, timedelta
from hashlib import sha256
from operator import itemgetter
from typing import Any, Literal
from uuid import uuid4

from dify_agent.protocol.workbench_control import (
    CompactionState,
    GoalState,
    GoalUpdate,
    PlanState,
    TodoWrite,
    WorkbenchControlState,
    change_goal,
    finish_goal,
    parse_command,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from werkzeug.exceptions import BadRequest, Conflict, Forbidden

from configs import dify_config
from core.db.session_factory import session_factory
from libs.datetime_utils import naive_utc_now
from models.workbench import WorkbenchChat, WorkbenchCommand, WorkbenchControl, WorkbenchRun


class AgentControlPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    account_id: str
    app_id: str
    workbench_run_id: str
    backend_run_id: str
    action: Literal["read", "apply_plan", "todo_write", "update_goal", "review_plan", "compact_result"] = "read"
    request_key: str | None = Field(default=None, max_length=128)
    data: dict[str, Any] = Field(default_factory=dict)


def _tick_goal(goal: GoalState, now: float) -> None:
    """Persist active time at phase boundaries; reading never advances stored time."""
    if goal.started_at is None:
        goal.started_at = now
    if goal.phase == "active":
        if goal.active_since is None:
            goal.active_since = now
    elif goal.active_since is not None:
        goal.elapsed_seconds += max(0, now - goal.active_since)
        goal.active_since = None


def _tick_plan(plan: PlanState, now: float) -> None:
    """Persist planning time at approval; reads leave the saved clock unchanged."""
    if plan.started_at is None:
        return
    if plan.active and not plan.completed:
        if plan.active_since is None:
            plan.active_since = now
    elif plan.active_since is not None:
        plan.elapsed_seconds += max(0, now - plan.active_since)
        plan.active_since = None


def _restore_goal_clock(session, chat, goal: GoalState) -> None:
    """Recover pre-clock goals from their durable command lifecycle snapshots."""
    snapshots = []
    for record in session.scalars(
        select(WorkbenchCommand).where(
            WorkbenchCommand.chat_id == chat.id,
            WorkbenchCommand.tenant_id == chat.tenant_id,
            WorkbenchCommand.account_id == chat.account_id,
            WorkbenchCommand.result.contains(goal.id),
        )
    ):
        state = json.loads(record.result).get("state", {})
        value = state.get("goal") or {}
        if value.get("id") == goal.id:
            snapshots.append((state.get("revision", 0), record.created_at, value["phase"]))
    if not snapshots:
        return
    phase = goal.phase
    last_time = 0.0
    for _, created_at, saved_phase in sorted(snapshots, key=itemgetter(0)):
        last_time = max(last_time, created_at.replace(tzinfo=UTC).timestamp())
        goal.phase = saved_phase
        _tick_goal(goal, last_time)
    goal.phase = phase
    # Human stop and terminal-error settlement can change phase without a command.
    run = session.get(WorkbenchRun, goal.last_run_id) if goal.last_run_id else None
    if run is not None and run.chat_id == chat.id:
        last_time = max(last_time, run.updated_at.replace(tzinfo=UTC).timestamp())
    _tick_goal(goal, last_time)


def load(session, chat) -> WorkbenchControlState:
    row = session.scalar(
        select(WorkbenchControl).where(
            WorkbenchControl.chat_id == chat.id,
            WorkbenchControl.tenant_id == chat.tenant_id,
            WorkbenchControl.account_id == chat.account_id,
        )
    )
    state = WorkbenchControlState.model_validate_json(row.state) if row else WorkbenchControlState()
    # The original public command always persisted this internal default; no
    # command exposed a user-selected limit. Lift it for existing goals too.
    # Do not reactivate blocked/paused goals without a user resume command.
    if state.goal and state.goal.max_rounds == 256:
        state.goal.max_rounds = None
    if state.goal and state.goal.started_at is None:
        _restore_goal_clock(session, chat, state.goal)
    return state


def save(session, chat, state: WorkbenchControlState):
    """The caller holds the chat lock; missing state is created exactly once."""
    row = session.scalar(select(WorkbenchControl).where(WorkbenchControl.chat_id == chat.id))
    if row is None:
        row = WorkbenchControl(
            tenant_id=chat.tenant_id,
            account_id=chat.account_id,
            chat_id=chat.id,
        )
        session.add(row)
    elif row.tenant_id != chat.tenant_id or row.account_id != chat.account_id:
        raise Forbidden()
    now = naive_utc_now()
    instant = now.replace(tzinfo=UTC).timestamp()
    if state.goal:
        _tick_goal(state.goal, instant)
    _tick_plan(state.plan, instant)
    row.state = state.model_dump_json()
    row.goal_active = bool(
        (state.goal and state.goal.phase == "active")
        or (state.compaction and state.compaction.phase in {"queued", "compacting"})
    )
    row.updated_at = now
    return state


def read(tenant_id, account_id, chat_id):
    from services.workbench.service import _chat, authorize

    authorize(tenant_id, account_id)
    with session_factory.create_session() as session:
        return load(session, _chat(session, tenant_id, account_id, chat_id)).model_dump(mode="json")


def _active(session, chat):
    from services.workbench.service import ACTIVE_STATUSES

    return session.scalar(
        select(WorkbenchRun)
        .where(
            WorkbenchRun.chat_id == chat.id,
            WorkbenchRun.tenant_id == chat.tenant_id,
            WorkbenchRun.account_id == chat.account_id,
            WorkbenchRun.status.in_(ACTIVE_STATUSES),
        )
        .with_for_update()
    )


def _record(session, chat, request_key, command, result):
    session.add(
        WorkbenchCommand(
            tenant_id=chat.tenant_id,
            account_id=chat.account_id,
            chat_id=chat.id,
            request_key=request_key,
            command=command,
            result=json.dumps(result, ensure_ascii=False),
        )
    )


def clear_todos(tenant_id, account_id, chat_id, *, request_key, expected_revision):
    """Clear the displayed generation without touching the goal or conversation."""
    from services.workbench.service import _chat, authorize

    authorize(tenant_id, account_id)
    fingerprint = json.dumps({"action": "clear_todos", "revision": expected_revision}, sort_keys=True)
    with session_factory.get_session_maker().begin() as session:
        chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
        previous = session.scalar(
            select(WorkbenchCommand).where(
                WorkbenchCommand.chat_id == chat.id, WorkbenchCommand.request_key == request_key
            )
        )
        state = load(session, chat)
        if previous:
            if previous.command != fingerprint:
                raise Conflict("操作编号已用于另一个操作")
        else:
            if state.revision != expected_revision:
                raise Conflict("任务清单已更新，请刷新后再清除")
            state.todos, state.todos_run_id = [], None
            state.revision += 1
            save(session, chat, state)
            _record(session, chat, request_key, fingerprint, {"state": state.model_dump(mode="json")})
        return state.model_dump(mode="json")


def issue(
    tenant_id, account_id, chat_id, *, command, request_key, expected_revision=None, files=None, resource_mentions=None
):
    """Execute a slash command without sending command/status text to a model."""
    from services.workbench.service import _chat, authorize

    authorize(tenant_id, account_id)
    try:
        parsed = parse_command(command)
    except ValueError as error:
        raise BadRequest(str(error)) from error
    files = files or []
    if files and not (
        parsed.action in {"create", "edit"}
        or (parsed.name == "plan" and parsed.action == "on")
        or (parsed.name == "compact" and parsed.text)
    ):
        raise BadRequest("附件需要随目标内容或计划内容一起发送")
    from services.workbench.mentions import ResourceMentions, resolve_mentions

    refs = ResourceMentions.model_validate(resource_mentions or {}).model_dump()
    if any(refs.values()):
        from services.workbench.personal_mcp import catalog as personal_mcp_catalog
        from services.workbench.resources import personal_skill_catalog
        from services.workbench.service import template

        personal = (
            personal_skill_catalog(tenant_id, account_id)
            if any(name.startswith("personal:") for name in refs["skills"])
            else []
        )
        personal_mcp = (
            personal_mcp_catalog(tenant_id, account_id)
            if any(name.startswith("personal:mcp:") for name in refs["tools"])
            else []
        )
        resolve_mentions(
            template(tenant_id, account_id)["soul"], refs, personal_skills=personal, personal_mcp=personal_mcp
        )
    fingerprint = json.dumps({"command": command, "files": files, "resource_mentions": refs}, sort_keys=True)
    result: dict[str, Any]
    with session_factory.get_session_maker().begin() as session:
        chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
        existing = session.scalar(
            select(WorkbenchCommand).where(
                WorkbenchCommand.chat_id == chat.id,
                WorkbenchCommand.request_key == request_key,
            )
        )
        if existing:
            if existing.command != fingerprint:
                raise Conflict("命令编号已用于另一条命令")
            result = json.loads(existing.result)
        else:
            state = load(session, chat)
            if expected_revision is not None and state.revision != expected_revision:
                raise Conflict("会话模式已改变，请刷新后重试")
            active = _active(session, chat)
            run_request = None
            if parsed.name == "goal":
                try:
                    state = change_goal(state, parsed)
                except ValueError as error:
                    raise Conflict(str(error)) from error
                if state.goal and parsed.action in {"create", "edit"} and resource_mentions is not None:
                    state.goal.resource_mentions = refs
                if state.goal and parsed.action == "create":
                    state.goal.source_request_key = request_key
                message = "当前没有目标" if state.goal is None else f"目标：{state.goal.objective}"
                if state.goal and (parsed.action == "create" or files):
                    run_request = {
                        "query": parsed.text,
                        "files": files,
                        "queue_when_busy": True,
                        "_control": {"kind": "goal_input", "goal_id": state.goal.id},
                    }
            elif parsed.name == "plan":
                if state.plan.review_run_id and active and active.status == "waiting_input":
                    raise Conflict("请先在计划卡片中选择开始执行或继续规划")
                state.plan = PlanState(
                    active=parsed.action == "on",
                    pending=active is not None,
                    version=state.plan.version,
                    objective=parsed.text if parsed.action == "on" else "",
                    started_at=naive_utc_now().replace(tzinfo=UTC).timestamp() if parsed.action == "on" else None,
                )
                state.revision += 1
                message = "已进入计划模式" if state.plan.active else "已退出计划模式"
                if parsed.text or files:
                    run_request = {
                        "query": parsed.text or "请根据附件制定计划",
                        "files": files,
                        "queue_when_busy": True,
                    }
            else:
                from services.workbench.followups import pending_runs
                from services.workbench.recovery import pending_condition

                if (
                    active
                    or pending_runs(session, chat)
                    or session.scalar(
                        select(WorkbenchRun.id).where(
                            WorkbenchRun.chat_id == chat.id,
                            pending_condition(),
                        )
                    )
                ):
                    raise Conflict("请在当前任务结束或暂停后压缩上下文")
                if not chat.conversation_id:
                    raise Conflict("当前会话还没有可压缩的上下文")
                identifier = str(uuid4())
                state.compaction = CompactionState(id=identifier, phase="queued")
                state.revision += 1
                message = "已开始压缩上下文"
                run_request = {
                    "query": parsed.text or "压缩上下文",
                    "files": files,
                    "_control": {"kind": "compact", "id": identifier, "continue_after": bool(parsed.text)},
                }
            if run_request is not None:
                run_request["resource_mentions"] = refs
            save(session, chat, state)
            result = {
                "state": state.model_dump(mode="json"),
                "message": message,
                "request": run_request,
                "version": chat.version,
                "run": None,
            }
            _record(session, chat, request_key, fingerprint, result)
    # Enqueue has its own durable request-key transaction. Retrying after either
    # commit fills in the same run; a failed publication is scheduler-recoverable.
    if result.get("request") and result.get("run") is None:
        _enqueue_command_request(tenant_id, account_id, chat_id, request_key, result, parsed.name)
    if parsed.name == "goal" and parsed.action in {"create", "resume"}:
        drive_goal(tenant_id, account_id, chat_id)
    result["state"] = read(tenant_id, account_id, chat_id)
    return {key: result[key] for key in ("state", "message", "run")}


def _enqueue_command_request(tenant_id, account_id, chat_id, request_key, result, command):
    """Fill a committed command's admission gap without recursively driving it.

    Its immutable text/files survive retries. Until a run exists, a goal uses
    the owner's current authorized configuration; enqueue rechecks that version
    under the chat lock. Already committed runs retain their frozen selection.
    """
    from services.workbench.service import _chat, enqueue

    values = dict(result["request"])
    private_control = values.pop("_control", None)
    if private_control and private_control.get("kind") == "goal_input":
        with session_factory.create_session() as session:
            result["version"] = _chat(session, tenant_id, account_id, chat_id).version
    result["run"] = enqueue(
        tenant_id,
        account_id,
        chat_id,
        result["version"],
        "command:" + sha256(request_key.encode()).hexdigest(),
        {**values, "activity_protocol": 1},
        control=private_control,
        command=command,
    )
    with session_factory.get_session_maker().begin() as session:
        _chat(session, tenant_id, account_id, chat_id, lock=True)
        row = session.scalar(
            select(WorkbenchCommand).where(
                WorkbenchCommand.chat_id == chat_id,
                WorkbenchCommand.tenant_id == tenant_id,
                WorkbenchCommand.account_id == account_id,
                WorkbenchCommand.request_key == request_key,
            )
        )
        if row is None:
            raise Conflict("命令记录已改变，请刷新会话后重试")
        row.result = json.dumps(result, ensure_ascii=False)
    return result["run"]


def detach_goal_input(session, chat, message, target=None):
    """Resolve the initial goal input when its queued message is removed/steered."""
    state = load(session, chat)
    goal = state.goal
    if not goal or goal.rounds_started or not goal.source_request_key:
        return
    if message.request_key != "command:" + sha256(goal.source_request_key.encode()).hexdigest():
        return
    if target is None:
        goal.phase, goal.reason = "paused", "目标首轮消息已移出队列，请调整后继续"
        goal.source_request_key = None
        goal.revision += 1
    else:
        # This user input now belongs to the active task's steering history.
        # Bind completion/recovery to that task rather than creating it again.
        goal.rounds_started, goal.last_run_id = 1, target.id
        data = json.loads(target.payload)
        data["control"] = {
            "kind": "goal",
            "goal_id": goal.id,
            "goal_revision": goal.revision,
            "round": 1,
            "source": "user",
        }
        target.payload = json.dumps(data)
    state.revision += 1
    save(session, chat, state)


def admit_run(session, chat, run, control):
    """Bind server-created mode work to the exact goal/compact generation."""
    state = load(session, chat)
    payload = json.loads(run.payload)
    if control:
        if control["kind"] == "goal_input":
            goal = state.goal
            if not goal or goal.id != control["goal_id"] or goal.phase == "complete":
                raise Conflict("目标已清除或替换，此条目标输入不再执行")
            if "recovery_revision" in control and (
                goal.phase != "active" or goal.revision != control["recovery_revision"]
            ):
                raise Conflict("目标已改变，此条首轮恢复不再执行")
            # Human inputs are immutable messages. A later goal edit changes
            # the current objective, not the submitted message's displayed text.
            goal.rounds_started += 1
            goal.last_run_id = run.id
            control = {
                "kind": "goal",
                "goal_id": goal.id,
                "goal_revision": goal.revision,
                "round": goal.rounds_started,
                "source": "user",
            }
            payload["is_continuation"] = False
        elif control["kind"] == "goal":
            goal = state.goal
            if (
                not goal
                or goal.phase != "active"
                or state.plan.active
                or (goal.id, goal.revision) != (control["goal_id"], control["goal_revision"])
            ):
                raise Conflict("目标已改变，此轮自动执行已取消")
            if goal.rounds_started != control["round"] - 1 or (
                goal.max_rounds is not None and goal.rounds_started >= goal.max_rounds
            ):
                raise Conflict("目标执行轮次已改变")
            goal.rounds_started += 1
            goal.last_run_id = run.id
            payload["is_continuation"] = goal.rounds_started > 1
        elif control["kind"] == "compact":
            if not state.compaction or state.compaction.id != control["id"] or state.compaction.phase != "queued":
                raise Conflict("上下文压缩请求已改变")
            payload["is_continuation"] = False
        payload["control"] = control
    elif not payload.get("is_continuation") and not payload.get("continue_run_id"):
        state.todos, state.todos_run_id = [], run.id
    state.revision += 1
    save(session, chat, state)
    run.payload = json.dumps(payload)


def with_commands(session, runs, values):
    """Recover immutable command text and tags without rewriting stored history.

    Legacy attached goals stored a placeholder as their run query. Its own
    command is the source of truth; the current goal may have since been edited.
    """
    pending = {
        run.request_key: value
        for run, value in zip(runs, values)
        if run.request_key.startswith("command:") and (not value.get("command") or value.get("query") == "目标参考附件")
    }
    if not pending or not runs:
        return values
    owner = runs[0]
    records = session.scalars(
        select(WorkbenchCommand).where(
            WorkbenchCommand.chat_id == owner.chat_id,
            WorkbenchCommand.tenant_id == owner.tenant_id,
            WorkbenchCommand.account_id == owner.account_id,
        )
    )
    for row in records:
        key = "command:" + sha256(row.request_key.encode()).hexdigest()
        value = pending.get(key)
        if value is None:
            continue
        try:
            recorded = json.loads(row.command)
            command = parse_command(recorded["command"])
        except (KeyError, TypeError, ValueError):
            continue
        value["command"] = command.name
        if command.name == "goal" and command.action in {"create", "edit"}:
            value["query"] = command.text
    return values


def drive_goal(tenant_id, account_id, chat_id):
    """Admit one goal round only when the conversation is quiescent.

    Unlike DSH's process-local driver, the distributed worker stores activation
    as the active phase and uses the existing DB/Redis execution lease. A restart
    therefore resumes authorized goals without admitting concurrent rounds.
    """
    from services.workbench import service
    from services.workbench.followups import pending_runs
    from services.workbench.recovery import pending_condition

    service.authorize(tenant_id, account_id)
    initial_command = None
    with session_factory.get_session_maker().begin() as session:
        chat = service._chat(session, tenant_id, account_id, chat_id, lock=True)
        state = load(session, chat)
        goal = state.goal
        if (
            not goal
            or goal.phase != "active"
            or (state.plan.active and not (goal.rounds_started == 0 and goal.source_request_key))
            or (state.compaction and state.compaction.phase in {"queued", "compacting"})
            or _active(session, chat)
            or pending_runs(session, chat)
        ):
            return None
        if session.scalar(select(WorkbenchRun.id).where(WorkbenchRun.chat_id == chat.id, pending_condition())):
            return None
        if goal.max_rounds is not None and goal.rounds_started >= goal.max_rounds:
            goal.phase, goal.reason = "blocked", f"已达到 {goal.max_rounds} 轮自动执行上限"
            goal.revision += 1
            state.revision += 1
            save(session, chat, state)
            return None
        current = goal.model_copy(deep=True)
        version = chat.version
        if current.rounds_started == 0 and current.source_request_key:
            record = session.scalar(
                select(WorkbenchCommand).where(
                    WorkbenchCommand.chat_id == chat.id,
                    WorkbenchCommand.tenant_id == tenant_id,
                    WorkbenchCommand.account_id == account_id,
                    WorkbenchCommand.request_key == current.source_request_key,
                )
            )
            if record is None:
                return None
            first = session.scalar(
                select(WorkbenchRun).where(
                    WorkbenchRun.chat_id == chat.id,
                    WorkbenchRun.tenant_id == tenant_id,
                    WorkbenchRun.account_id == account_id,
                    WorkbenchRun.request_key == "command:" + sha256(current.source_request_key.encode()).hexdigest(),
                )
            )
            if first is not None:
                # A durable message is never a missing enqueue. Queue actions
                # normally reconcile the goal atomically; fail closed if an
                # interrupted transition left an unadmitted terminal record.
                goal.phase, goal.reason = "paused", "目标首轮消息已结束或移出队列，请核对后继续"
                goal.source_request_key = None
                goal.revision += 1
                state.revision += 1
                save(session, chat, state)
                return None
            initial_command = json.loads(record.result)
            if not initial_command.get("request"):
                return None
            initial_command["request"]["_control"]["recovery_revision"] = current.revision
    if initial_command is not None:
        # Recover a crash between saving a goal command and enqueuing its first
        # message. Never re-enter issue/drive_goal through cached command state.
        try:
            return _enqueue_command_request(
                tenant_id,
                account_id,
                chat_id,
                current.source_request_key,
                initial_command,
                "goal",
            )
        except Conflict:
            return None
    number = current.rounds_started + 1
    try:
        return service.enqueue(
            tenant_id,
            account_id,
            chat_id,
            version,
            f"goal:{current.id}:{current.revision}:{number}",
            {
                "query": current.objective if number == 1 else "继续完成目标：" + current.objective,
                "activity_protocol": 1,
                "followup_protocol": 1,
                "resource_mentions": current.resource_mentions,
            },
            control={"kind": "goal", "goal_id": current.id, "goal_revision": current.revision, "round": number},
        )
    except Conflict:
        # A user send/mode change or another driver won admission. The next
        # completed run or periodic reconciliation will re-evaluate fresh state.
        return None


def active_goal_chats():
    # Rotate even busy goals so one page of long-running conversations cannot
    # starve later owners. The compact operation shares this recovery index.
    with session_factory.get_session_maker().begin() as session:
        rows = list(
            session.scalars(
                select(WorkbenchControl)
                .join(WorkbenchChat, WorkbenchChat.id == WorkbenchControl.chat_id)
                .where(
                    WorkbenchChat.deleted == 0,
                    WorkbenchControl.goal_active.is_(True),
                )
                .order_by(WorkbenchControl.updated_at)
                .limit(100)
                .with_for_update(skip_locked=True, of=WorkbenchControl)
            )
        )
        for row in rows:
            row.updated_at = naive_utc_now()
        return [(row.tenant_id, row.account_id, row.chat_id) for row in rows]


def settle(tenant_id, account_id, chat_id):
    """Reconcile terminal failures before considering a new automatic round."""
    from services.workbench.recovery import recovery_dto
    from services.workbench.service import _chat

    with session_factory.get_session_maker().begin() as session:
        chat = _chat(session, tenant_id, account_id, chat_id, lock=True)
        state = load(session, chat)
        # Settle the run belonging to each control generation. A newer ordinary
        # message must not hide a failed goal round or leave compaction stuck.
        goal_run = session.get(WorkbenchRun, state.goal.last_run_id) if state.goal and state.goal.last_run_id else None
        compact_run = (
            session.scalar(
                select(WorkbenchRun)
                .where(WorkbenchRun.chat_id == chat.id, WorkbenchRun.payload.contains(state.compaction.id))
                .order_by(WorkbenchRun.created_at.desc(), WorkbenchRun.id.desc())
                .limit(1)
            )
            if state.compaction and state.compaction.phase in {"queued", "compacting"}
            else None
        )
        changed = False
        if state.compaction and state.compaction.phase == "queued":
            command_row = session.scalar(
                select(WorkbenchCommand)
                .where(
                    WorkbenchCommand.chat_id == chat.id,
                    WorkbenchCommand.result.contains(state.compaction.id),
                )
                .order_by(WorkbenchCommand.created_at.desc())
                .limit(1)
            )
            if not compact_run and command_row and command_row.created_at < naive_utc_now() - timedelta(minutes=1):
                state.compaction.phase = "failed"
                state.compaction.message = "压缩任务未能入队，原上下文保留，请重新执行 /compact"
                changed = True
        for controlled_run in (goal_run, compact_run):
            if controlled_run is None or controlled_run.chat_id != chat.id:
                continue
            payload = json.loads(controlled_run.payload)
            if controlled_run.status not in {"failed", "interrupted", "cancelled"} or (recovery_dto(payload) or {}).get(
                "pending"
            ):
                continue
            current = payload.get("control", {})
            if (
                state.goal
                and state.goal.phase == "active"
                and current.get("goal_id") == state.goal.id
                and current.get("goal_revision") == state.goal.revision
            ):
                state.goal.phase, state.goal.reason = "blocked", controlled_run.error or "目标执行中断，请检查后继续"
                state.goal.revision += 1
                changed = True
            if (
                state.compaction
                and current.get("kind") == "compact"
                and current.get("id") == state.compaction.id
                and state.compaction.phase in {"queued", "compacting"}
            ):
                state.compaction.phase, state.compaction.message = (
                    "failed",
                    controlled_run.error or "压缩任务已中断，请刷新后核对上下文状态",
                )
                changed = True
        if changed:
            state.revision += 1
            save(session, chat, state)


def agent_control(payload: AgentControlPayload):
    """Only the current execution ticket can publish model-authored state."""
    from services.workbench.event_log import append_locked, notify
    from services.workbench.recovery import locked_run

    if not dify_config.WORKBENCH_ENABLED:
        raise Forbidden()
    cursor = None
    with session_factory.get_session_maker().begin() as session:
        chat, run = locked_run(session, payload.tenant_id, payload.account_id, payload.workbench_run_id)
        if chat.app_id != payload.app_id or run.status != "running" or run.backend_run_id != payload.backend_run_id:
            raise Forbidden("当前执行已结束或身份不匹配")
        state = load(session, chat)
        run_data = json.loads(run.payload)
        identifier = (
            "agent:" + sha256(f"{payload.backend_run_id}:{payload.request_key}".encode()).hexdigest()
            if payload.request_key
            else None
        )
        if payload.action != "read" and identifier is None:
            raise BadRequest("状态更新缺少幂等编号")
        if payload.action != "read" and identifier:
            previous = session.scalar(
                select(WorkbenchCommand).where(
                    WorkbenchCommand.chat_id == chat.id,
                    WorkbenchCommand.request_key == identifier,
                )
            )
            if previous:
                if previous.command != payload.model_dump_json():
                    raise Conflict("状态更新编号已用于另一个操作")
                return json.loads(previous.result)
        if payload.action == "todo_write":
            if state.plan.active:
                raise Conflict("计划模式请提交完整方案供用户审阅；任务清单仅用于批准后的实施进度")
            todos = TodoWrite.model_validate(payload.data).todos
            if todos == state.todos:
                # An identical list has no new progress to publish. Retain the
                # request's idempotency record without a new UI event/revision.
                result = {"state": state.model_dump(mode="json"), "control": run_data.get("control")}
                _record(session, chat, identifier, payload.model_dump_json(), result)
                return result
            state.todos = todos
            state.todos_run_id = run.id
        elif payload.action == "update_goal":
            if state.plan.active:
                raise Conflict("计划尚未批准，请先提交完整方案供用户审阅")
            try:
                state = finish_goal(state, **GoalUpdate.model_validate(payload.data).model_dump())
            except (ValueError, TypeError) as error:
                raise Conflict(str(error)) from error
        elif payload.action == "apply_plan":
            # A read and a new user selection may race. The model uses the
            # returned desired state; acknowledge only the matching revision.
            if payload.data.get("revision") == state.revision:
                state.plan.pending = False
        elif payload.action == "review_plan":
            plan = payload.data.get("plan", "")
            if not isinstance(plan, str):
                raise BadRequest("请提供以标题开头的完整 Markdown 计划")
            try:
                state.plan.submit(plan, run.id)
            except ValueError as error:
                raise Conflict(str(error)) from error
        elif payload.action == "compact_result":
            result = CompactionState.model_validate(payload.data)
            if not state.compaction or result.id != state.compaction.id:
                raise Conflict("压缩请求已改变")
            state.compaction = result
        if payload.action != "read":
            state.revision += 1
            save(session, chat, state)
            cursor = append_locked(
                session,
                run,
                {
                    "event": "workbench_control",
                    "state": state.model_dump(mode="json"),
                    "backend_run_id": payload.backend_run_id,
                    "source_event_id": identifier or str(uuid4()),
                },
            )
        result = {"state": state.model_dump(mode="json"), "control": run_data.get("control")}
        if payload.action != "read" and identifier:
            _record(session, chat, identifier, payload.model_dump_json(), result)
    if cursor:
        notify(payload.workbench_run_id, cursor)
    return result


def review_answer(session, chat, run, action, pending):
    """The user approves the exact stored plan; no timer can approve it."""
    state = load(session, chat)
    if action not in {"approve", "keep_planning"}:
        raise BadRequest("请选择开始执行或继续规划")
    try:
        state.plan.answer(
            run_id=run.id,
            version=pending.get("metadata", {}).get("plan_version", 0),
            plan=pending["args"].get("markdown", ""),
            approve=action == "approve",
        )
    except ValueError as error:
        raise Conflict(str(error)) from error
    state.revision += 1
    save(session, chat, state)


def pause_goal(session, chat):
    state = load(session, chat)
    if state.goal and state.goal.phase == "active":
        state.goal.phase = "paused"
        state.goal.revision += 1
        state.revision += 1
        save(session, chat, state)
