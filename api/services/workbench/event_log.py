"""Ordered, durable workbench events; Redis only wakes readers of the journal.

New protocol runs append bounded records under their owning run's row lock.
Readers use the same database sequence for history and SSE. A crash between the
commit and notification therefore delays delivery, but cannot lose progress.
Legacy runs keep their existing Redis cursor / event_log contract.
"""

import hashlib
import json
import logging
import time
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import object_session
from werkzeug.exceptions import NotFound

from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from models.workbench import WorkbenchChat, WorkbenchRun, WorkbenchRunEvent
from services.workbench.scheduler import event_key

logger = logging.getLogger(__name__)
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
REDUNDANT_EVENTS = frozenset({"agent_thought", "agent_message"})


def uses_journal(run, payload=None):
    return (payload if payload is not None else json.loads(run.payload)).get("activity_protocol") == 1


def owned_statement(tenant_id, account_id, run_id):
    return (
        select(WorkbenchRun)
        .join(WorkbenchChat, WorkbenchChat.id == WorkbenchRun.chat_id)
        .where(
            WorkbenchRun.id == run_id,
            WorkbenchRun.tenant_id == tenant_id,
            WorkbenchRun.account_id == account_id,
            WorkbenchChat.tenant_id == tenant_id,
            WorkbenchChat.account_id == account_id,
            WorkbenchChat.deleted == 0,
        )
    )


def append_locked(session, run, item):
    """Caller holds the run row lock; never replace a previous event or do network I/O here."""
    if json.loads(run.payload).get("activity_closed"):
        return None
    if run.status in TERMINAL_STATUSES and item.get("event") != "workbench_end":
        return None
    if item.get("event") in REDUNDANT_EVENTS:
        # Protocol 1 already records the same content as ordered narrative/tool events.
        message_id = item.get("message_id")
        payload = json.loads(run.payload)
        if message_id and message_id not in payload.get("message_ids", []):
            payload.setdefault("message_ids", []).append(message_id)
            run.payload = json.dumps(payload)
        return None
    _append_tool_knowledge(session, run, item)
    stored = _append_record_locked(session, run, item)
    if item.get("event") == "workbench_end" and item.get("status") in TERMINAL_STATUSES:
        payload = json.loads(run.payload)
        payload["activity_closed"] = True
        run.payload = json.dumps(payload)
        session.flush()
    return stored


def _append_record_locked(session, run, item):
    """Append one record; only the closing owner may flush known terminal results."""
    source = item.get("source_event_id")
    identity = f"{item.get('backend_run_id', '')}:{source}" if source else str(uuid4())
    identity = hashlib.sha256(identity.encode()).hexdigest()
    existing = session.scalar(
        select(WorkbenchRunEvent).where(
            WorkbenchRunEvent.run_id == run.id,
            WorkbenchRunEvent.event_key == identity,
        )
    )
    if existing is not None:
        return json.loads(existing.payload)
    sequence = (
        session.scalar(select(func.max(WorkbenchRunEvent.sequence)).where(WorkbenchRunEvent.run_id == run.id)) or 0
    ) + 1
    stored = {**item, "workbench_run_id": run.id, "_id": f"{sequence}-0", "_sequence": sequence}
    session.add(
        WorkbenchRunEvent(
            run_id=run.id,
            sequence=sequence,
            event_key=identity,
            payload=json.dumps(stored, ensure_ascii=False),
        )
    )
    payload = json.loads(run.payload)
    if item.get("event") == "workbench_context":
        payload["context_usage"] = stored
        run.payload = json.dumps(payload)
    message_id = item.get("message_id")
    if isinstance(message_id, str) and message_id and message_id not in payload.get("message_ids", []):
        payload.setdefault("message_ids", []).append(message_id)
        run.payload = json.dumps(payload)
    session.flush()
    return stored


def notify(run_id, item):
    """A notification failure is recoverable: SSE also polls the committed journal."""
    try:
        redis_client.xadd(event_key(run_id), {"data": json.dumps(item, ensure_ascii=False)})
        redis_client.expire(event_key(run_id), 7 * 86400)
    except Exception:
        logger.warning("Workbench event notification delayed for run %s", run_id, exc_info=True)


def _append_tool_knowledge(session, run, item):
    terminal = item.get("event") == "workbench_end" or (
        item.get("event") == "workbench_status" and item.get("status") in ("waiting_input", "environment_update")
    )
    data = item.get("data")
    if not terminal and (
        item.get("event") != "workbench_activity"
        or not isinstance(data, dict)
        or data.get("tool_name") != "knowledge_base_search"
        or data.get("stage") not in ("returned", "error")
    ):
        return
    output = data.get("output") if isinstance(data, dict) else None
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return
    if not terminal and (not isinstance(output, dict) or not output.get("search_id")):
        return
    search_id = output.get("search_id") if isinstance(output, dict) else None
    backend_run_id = run.backend_run_id if terminal else item.get("backend_run_id")
    for knowledge in json.loads(run.payload).get("knowledge_events", []):
        if (
            (terminal or knowledge.get("search_id") == search_id)
            and knowledge.get("status") in ("returned", "error")
            and knowledge.get("backend_run_id") == backend_run_id
        ):
            _append_record_locked(session, run, knowledge)


def append_event(run_id, item, *, expected_backend_run_id=None):
    with session_factory.get_session_maker().begin() as session:
        run = session.scalar(select(WorkbenchRun).where(WorkbenchRun.id == run_id).with_for_update())
        if run is None:
            raise NotFound()
        if expected_backend_run_id and run.backend_run_id != expected_backend_run_id:
            return None
        journal = uses_journal(run)
        if journal:
            item = append_locked(session, run, item)
            if item is None:
                return None
    if journal:
        notify(run_id, item)
        return item["_id"]
    identifier = redis_client.xadd(event_key(run_id), {"data": json.dumps(item)})
    redis_client.expire(event_key(run_id), 7 * 86400)
    return identifier.decode() if isinstance(identifier, bytes) else str(identifier)


def history_events(run):
    return history_snapshot(run)[0]


def history_snapshot(run):
    session = object_session(run)
    if session is not None:
        return _history_snapshot(session, run.id)
    with session_factory.create_session() as session:
        return _history_snapshot(session, run.id)


def _history(session, run_id):
    return _history_snapshot(session, run_id)[0]


def _history_snapshot(session, run_id):
    # Attempt endings belong to stream control. The DTO's current run status
    # describes history, including a task that has resumed after a pause.
    items = []
    cursor = 0
    for sequence, value in session.execute(
        select(WorkbenchRunEvent.sequence, WorkbenchRunEvent.payload)
        .where(WorkbenchRunEvent.run_id == run_id)
        .order_by(WorkbenchRunEvent.sequence)
    ):
        cursor = sequence
        item = json.loads(value)
        if item.get("event") not in REDUNDANT_EVENTS | {"workbench_end"}:
            items.append(item)
    return compact_narratives(items), f"{cursor}-0"


def compact_narratives(items):
    """Coalesce adjacent deltas without changing the first event identity or ordering."""
    result: list[dict[str, object]] = []
    parts: list[str] = []
    narrative: dict[str, object] | None = None
    key = None
    for item in items:
        data = item.get("data") if item.get("event") == "workbench_activity" else None
        if isinstance(data, dict) and data.get("kind") in {"text", "reasoning"} and isinstance(data.get("text"), str):
            current = (data.get("kind"), data.get("segment_id"))
            if current == key:
                parts.append(data["text"])
                continue
            if narrative is not None:
                narrative["text"] = "".join(parts)
            key, parts, narrative = current, [data["text"]], dict(data)
            result.append({**item, "data": narrative})
        else:
            if narrative is not None:
                narrative["text"] = "".join(parts)
            key, parts, narrative = None, [], None
            result.append(item)
    if narrative is not None:
        narrative["text"] = "".join(parts)
    return result


def read_state(tenant_id, account_id, run_id):
    """Authorize stream/control requests without loading the event transcript."""
    with session_factory.create_session() as session:
        run = session.scalar(owned_statement(tenant_id, account_id, run_id))
        if run is None:
            raise NotFound()
        return {
            "status": run.status,
            "error": run.error,
            "task_id": run.task_id,
            "activity_protocol": 1 if uses_journal(run) else 0,
        }


def read_page(tenant_id, account_id, run_id, *, after=0, limit=100):
    """Recheck the full owner chain on every read without materializing the whole transcript."""
    with session_factory.create_session() as session:
        run = session.scalar(owned_statement(tenant_id, account_id, run_id))
        if run is None:
            raise NotFound()
        rows = list(
            session.scalars(
                select(WorkbenchRunEvent.payload)
                .where(
                    WorkbenchRunEvent.run_id == run.id,
                    WorkbenchRunEvent.sequence > after,
                )
                .order_by(WorkbenchRunEvent.sequence)
                .limit(min(max(limit, 1), 100))
            )
        )
        return [json.loads(value) for value in rows], run.status, run.error


def stream_events(tenant_id, account_id, run_id, *, after=0):
    """Read committed history and live events through one cursor; never replay the task."""
    live = {"queued", "running", "environment_update", "environment_installing", "stopping"}
    while True:
        items, status, error = read_page(tenant_id, account_id, run_id, after=after)
        for item in items:
            after = item["_sequence"]
            # Previous native attempts end while their logical task continues.
            # Emit a terminal record only after all committed pages are drained.
            if item.get("event") not in REDUNDANT_EVENTS | {"workbench_end"}:
                yield item
        if len(items) == 100:
            continue
        if status not in live:
            yield {"event": "workbench_end", "status": status, "error": error}
            return
        try:
            redis_client.xread({event_key(run_id): "$"}, count=1, block=1000)
        except Exception:
            time.sleep(1)
        yield None
