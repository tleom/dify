from controllers.console.workbench import WorkbenchChatEnvelopeResponse, WorkbenchRunEnvelopeResponse
from libs.helper import dump_response


def test_history_responses_preserve_context_usage_without_stream_events() -> None:
    usage = {
        "event": "workbench_context",
        "phase": "usage",
        "model": "provider::model",
        "used_tokens": 11054,
        "window_tokens": 1000000,
        "estimated": False,
        "_id": "12-0",
    }
    run = {
        "id": "run",
        "chat_id": "chat",
        "revision_id": "revision",
        "version": 1,
        "query": "question",
        "status": "completed",
        "events": [],
        "context_usage": usage,
    }
    assert dump_response(WorkbenchRunEnvelopeResponse, {"data": run})["data"]["context_usage"] == usage
    chat = {
        "id": "chat",
        "title": "title",
        "version": 1,
        "pinned": False,
        "template_snapshot_id": "snapshot",
        "selection": {"model": "provider::model"},
        "runs": [run],
    }
    assert dump_response(WorkbenchChatEnvelopeResponse, {"data": chat})["data"]["runs"][0]["context_usage"] == usage
    run.pop("context_usage")
    assert dump_response(WorkbenchRunEnvelopeResponse, {"data": run})["data"]["context_usage"] is None
