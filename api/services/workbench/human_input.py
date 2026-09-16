"""Keep skipped questions answerable through ordinary, durable follow-up messages."""

import json
from hashlib import sha256
from uuid import uuid4

from dify_agent.layers.ask_human.schema import AskHumanSelectedAction, AskHumanToolArgs, AskHumanToolResult
from sqlalchemy import or_, select
from werkzeug.exceptions import Conflict, NotFound

from core.db.session_factory import session_factory
from libs.datetime_utils import naive_utc_now
from models.workbench import WorkbenchRun


def validate_answer(raw_args, values, action):
    if sum(len(key) + len(value) for key, value in values.items()) > 100000:
        raise ValueError("输入内容过长")
    args = AskHumanToolArgs.model_validate(raw_args)
    allowed = {field.name: field for field in args.fields}
    if not set(values) <= allowed.keys():
        raise ValueError("输入包含未请求的字段")
    for field in args.fields:
        value = values.get(field.name)
        if field.required and not value:
            raise ValueError(f"请填写 {field.label}")
        if field.type == "select" and value is not None and value not in {option.value for option in field.options}:
            raise ValueError(f"{field.label} 选项无效")
    selected = None
    if action is not None:
        selected = next((item for item in args.actions if item.id == action), None)
        if selected is None:
            raise ValueError("操作选项无效")
    elif not args.fields and args.actions:
        raise ValueError("请选择操作")
    return AskHumanToolResult(
        status="submitted",
        values=values,
        action=AskHumanSelectedAction(id=selected.id, label=selected.label) if selected else None,
    )


def question_record(run_id, pending, result):
    raw = dict(pending["args"])
    raw["fields"] = raw.get("fields") or []
    raw["actions"] = raw.get("actions") or [{"id": "submit", "label": "Submit", "style": "primary"}]
    args = AskHumanToolArgs.model_validate(raw).model_dump(mode="json")
    source = [run_id, pending["tool_call_id"], args]
    backend_run_id = pending.get("backend_run_id")
    if backend_run_id:
        source.append(backend_run_id)
    identity = sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "request_id": identity,
        "tool_call_id": pending["tool_call_id"],
        "tool_name": "ask_human",
        "args": args,
        "backend_run_id": backend_run_id,
        "status": "submitted" if result.get("status") == "submitted" else "skipped",
        "values": result.get("values") or {},
        "action": (result.get("action") or {}).get("id"),
    }


def remember(run, payload, pending, result):
    if pending.get("tool_name") != "ask_human":
        return
    record = question_record(run.id, {**pending, "backend_run_id": run.backend_run_id}, result)
    payload.setdefault("human_input_history", {})[record["request_id"]] = record


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def history(run, events=None):
    """Recover older questions from owned journal events, without trusting client forms."""
    payload = json.loads(run.payload)
    if events is None:
        from services.workbench.event_log import history_snapshot, uses_journal

        events = history_snapshot(run)[0] if uses_journal(run, payload) else json.loads(run.event_log or "[]")
    calls: dict[object, dict] = {}
    for event in events:
        data = _object(event.get("data"))
        if event.get("event") == "workbench_activity" and data.get("tool_name") == "ask_human":
            call_id = data.get("tool_call_id") or data.get("call_id")
            key = data.get("call_id") or (event.get("backend_run_id"), call_id)
            item = calls.setdefault(
                key, {"tool_call_id": call_id, "tool_name": "ask_human", "backend_run_id": event.get("backend_run_id")}
            )
            if data.get("stage") == "started":
                item["args"] = _object(data.get("input"))
            elif data.get("stage") == "returned":
                item["result"] = _object(data.get("output"))
        elif event.get("event") == "agent_thought" and event.get("tool") == "ask_human":
            call_id = event.get("tool_call_id") or event.get("id")
            item = calls.setdefault(call_id, {"tool_call_id": call_id, "tool_name": "ask_human"})
            if event.get("tool_input"):
                raw = _object(event["tool_input"])
                item["args"] = _object(raw.get("ask_human", raw))
            if event.get("observation"):
                raw = _object(event["observation"])
                item["result"] = _object(raw.get("ask_human", raw))
    records = {}
    archived = payload.get("human_input_history", {})
    for item in calls.values():
        result = _object(item.get("result"))
        if (
            item.get("tool_call_id")
            and item.get("args")
            and result.get("status") in {"submitted", "cancelled", "timeout"}
        ):
            try:
                record = question_record(run.id, item, result)
            except ValueError:
                continue
            # Started events contain raw model arguments; the deferred request
            # adds defaults. The authoritative archived question wins after
            # normalization, including records written before this version.
            if any(
                value.get("tool_call_id") == record["tool_call_id"]
                and (not value.get("backend_run_id") or value["backend_run_id"] == record["backend_run_id"])
                and question_record(run.id, value, {})["args"] == record["args"]
                for value in archived.values()
            ):
                continue
            records[record["request_id"]] = record
    records.update(archived)
    return list(records.values())


def answer_text(record, result):
    args = AskHumanToolArgs.model_validate(record["args"])
    lines = ["补充此前已跳过的问题：", args.question]
    for field in args.fields:
        value = result.values.get(field.name)
        if value is not None:
            label = (
                next((option.label for option in field.options if option.value == value), value)
                if field.type == "select"
                else value
            )
            lines.append(f"{field.label}：{label}")
    if result.action:
        lines.append(f"选择：{result.action.label}")
    lines.append("请结合这份补充继续处理当前任务；已完成的操作无需重复。")
    return "\n".join(lines)


def supplement(tenant_id, account_id, run_id, request_id, values, action):
    from services.workbench import followups, service
    from services.workbench.branches import chat_runs, resolve_parent
    from services.workbench.recovery import locked_run, pending_condition

    service.authorize(tenant_id, account_id)
    with session_factory.get_session_maker().begin() as session:
        chat, source = locked_run(session, tenant_id, account_id, run_id)
        record = next((item for item in history(source) if item["request_id"] == request_id), None)
        if record is None:
            raise NotFound("补充信息请求不存在")
        result = validate_answer(record["args"], values, action)
        if record["status"] == "submitted":
            if record.get("supplement_run_id") and record["values"] == values and record.get("action") == action:
                message = session.get(WorkbenchRun, record["supplement_run_id"])
                if message is not None:
                    return service.run_dto(message)
            raise Conflict("这份补充信息已经提交，请刷新后查看")
        target = session.scalar(
            select(WorkbenchRun)
            .where(
                WorkbenchRun.chat_id == chat.id,
                WorkbenchRun.tenant_id == tenant_id,
                WorkbenchRun.account_id == account_id,
                or_(WorkbenchRun.status.in_(service.ACTIVE_STATUSES), pending_condition()),
            )
            .with_for_update()
        )
        waiting = followups.pending_runs(session, chat)
        previous = target or (waiting[-1] if waiting else chat_runs(session, chat)[-1])
        frozen = json.loads(previous.payload)
        direct = bool(
            target
            and target.status != "stopping"
            and frozen.get("followup_protocol") == 1
            and not (target.status == "running" and frozen.get("steering_closed_ticket") == target.backend_run_id)
        )
        parent = (
            {"branch_parent_run_id": target.id, "parent_message_id": None}
            if direct and target is not None
            else followups.queued_parent(session, chat, target, {})
            if target or waiting
            else resolve_parent(session, chat, {})
        )
        payload = {
            key: frozen[key]
            for key in (
                "effective_soul",
                "version",
                "queue_selection",
                "template_snapshot_id",
                "resource_mentions",
                "mentioned_resources",
                "mention_prompt",
                "inputs",
                "activity_protocol",
            )
            if key in frozen
        }
        payload.update(
            {
                **parent,
                "query": answer_text(record, result),
                "attempt": 0,
                "recovery": {"attempt": 0},
                "followup_protocol": 1,
                "input_supplement": {"run_id": source.id, "request_id": request_id},
            }
        )
        message = WorkbenchRun(
            id=str(uuid4()),
            tenant_id=tenant_id,
            account_id=account_id,
            chat_id=chat.id,
            revision_id=previous.revision_id,
            request_key=f"input-supplement:{request_id}",
            payload=json.dumps(payload),
            status=followups.WAITING,
            event_log="[]",
        )
        session.add(message)
        session.flush()
        if direct:
            followups._steer_locked(session, chat, message, target)
        # source and target may be the same row; retain steering added above.
        current = json.loads(source.payload)
        current.setdefault("human_input_history", {})[request_id] = {
            **record,
            "status": "submitted",
            "values": values,
            "action": action,
            "supplement_run_id": message.id,
        }
        source.payload = json.dumps(current)
        chat.updated_at = naive_utc_now()
        dto = service.run_dto(message)
    if dto["status"] == followups.WAITING:
        followups.advance(tenant_id, account_id, dto["chat_id"])
        with session_factory.create_session() as session:
            dto = service.run_dto(session.get(WorkbenchRun, dto["id"]))
    return dto
