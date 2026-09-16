"""Account-scoped durable context readings and ordered compaction events."""

import json

from configs import dify_config
from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from services.workbench.runtime import current_run
from services.workbench.scheduler import event_key


def record_context_status(tenant_id, conversation_id, account_id, public_event):
    if not dify_config.WORKBENCH_ENABLED:
        return
    with session_factory.get_session_maker().begin() as session:
        # This path writes the same payload as steering and final sealing. Read
        # it under the execution row lock so unrelated progress cannot replace
        # a supplement or seal committed while this event is being persisted.
        run = current_run(session, tenant_id, conversation_id, account_id, for_update=True)
        if run is None or run.tenant_id != tenant_id or run.account_id != account_id:
            return
        if run.backend_run_id != public_event.run_id:
            return
        payload = json.loads(run.payload)
        model = payload.get("effective_soul", {}).get("model", {})
        item = {
            "event": "workbench_context",
            "workbench_run_id": run.id,
            "model": f"{model.get('model_provider', '')}::{model.get('model', '')}",
            **public_event.data.model_dump(mode="json"),
        }
        if payload.get("activity_protocol") == 1:
            # The Agent App queues this with tools/text. Persistence happens at
            # the single ordered consumer, never ahead of its queued neighbors.
            return item
        cursor = redis_client.xadd(event_key(run.id), {"data": json.dumps(item)})
        redis_client.expire(event_key(run.id), 7 * 86400)
        item["_id"] = cursor.decode() if isinstance(cursor, bytes) else str(cursor)
        payload["context_usage"] = item
        if item["phase"] != "usage":
            payload.setdefault("context_events", []).append(item)
        run.payload = json.dumps(payload)


def merge_context_events(run, payload):
    """Retain live stream order after reload; legacy events precede cursor events."""
    events = [*json.loads(run.event_log), *payload.get("context_events", [])]

    def order(item):
        cursor = str(item.get("_id", "0-0")).split("-")
        return tuple(int(part) if part.isdigit() else 0 for part in cursor)

    return sorted(events, key=order)
