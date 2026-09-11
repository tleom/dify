"""Persist and stream each knowledge search within an account-owned run."""

import json

from sqlalchemy import select
from werkzeug.exceptions import Forbidden

from configs import dify_config
from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from models.workbench import WorkbenchChat, WorkbenchRun
from services.workbench.scheduler import event_key


def retrieval_event(request, status: str, *, results=None):
    caller = request.caller
    if not dify_config.WORKBENCH_ENABLED:
        raise Forbidden()
    with session_factory.get_session_maker().begin() as session:
        run = session.scalar(select(WorkbenchRun).join(WorkbenchChat, WorkbenchChat.id == WorkbenchRun.chat_id).where(
            WorkbenchRun.id == request.workbench_run_id, WorkbenchRun.tenant_id == caller.tenant_id,
            WorkbenchRun.account_id == caller.user_id, WorkbenchChat.app_id == caller.app_id,
            WorkbenchChat.tenant_id == caller.tenant_id, WorkbenchChat.account_id == caller.user_id,
            WorkbenchChat.deleted == 0,
        ).with_for_update())
        if run is None or run.status != "running":
            raise Forbidden("知识库检索任务已结束")
        payload = json.loads(run.payload)
        matching = [item for item in payload["effective_soul"].get("knowledge", {}).get("sets", [])
                    if {d["id"] for d in item["datasets"]} == set(request.dataset_ids)
                    and (item["query"]["mode"] == "generated_query"
                         or item["query"].get("value") == request.query)]
        if len(matching) != 1:
            raise Forbidden("知识库检索请求与任务配置不一致")
        item = matching[0]
        event = {
            "event": "workbench_knowledge",
            "id": f"knowledge:{run.id}:{request.workbench_search_id or item['id']}",
            "workbench_run_id": run.id, "name": item["name"], "query": request.query,
            "status": status, "results": results or [],
        }
        if status == "error":
            event["message"] = "知识库检索失败，请检查知识库的检索配置后重试。"
        events = payload.get("knowledge_events", [])
        payload["knowledge_events"] = [e for e in events if e["id"] != event["id"]] + [event]
        run.payload = json.dumps(payload)
    redis_client.xadd(event_key(request.workbench_run_id), {"data": json.dumps(event)})
    redis_client.expire(event_key(request.workbench_run_id), 7 * 86400)


def run_knowledge_events(run, payload):
    if "knowledge_events" in payload:
        return payload["knowledge_events"]
    if not payload.get("effective_soul", {}).get("knowledge", {}).get("sets"):
        return []
    # Older completed turns have no streamed retrieval record. Recover only
    # observations explicitly tagged with this run from its own native snapshot.
    from models.agent import AgentWorkspaceBinding
    from models.model import Conversation

    with session_factory.create_session() as session:
        snapshot = session.scalar(select(AgentWorkspaceBinding.session_snapshot)
            .join(Conversation, Conversation.agent_workspace_binding_id == AgentWorkspaceBinding.id)
            .join(WorkbenchChat, WorkbenchChat.conversation_id == Conversation.id)
            .where(WorkbenchChat.id == run.chat_id, WorkbenchChat.tenant_id == run.tenant_id,
                   WorkbenchChat.account_id == run.account_id))
    if not snapshot:
        return []
    events = []
    for layer in json.loads(snapshot).get("layers", []):
        for item in (layer.get("runtime_state") or {}).get("eager_results", []):
            if not item.get("set_id", "").endswith(":" + run.id):
                continue
            events.append({
                "event": "workbench_knowledge", "id": "knowledge:" + item["set_id"],
                "workbench_run_id": run.id, "name": item["set_name"], "query": item["query"],
                "status": "returned" if item["status"] in ("success", "empty") else "error",
                "observation": item["observation"], "results": [],
            })
    return events
