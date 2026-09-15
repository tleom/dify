from controllers.console.workbench import WorkbenchChatEnvelopeResponse, WorkbenchRunEnvelopeResponse
from libs.helper import dump_response


def test_steering_requires_a_captured_target_and_followup_queries_are_bounded() -> None:
    from uuid import uuid4

    import pytest
    from pydantic import ValidationError

    from controllers.console.workbench import WorkbenchFollowupsQuery, WorkbenchSteerPayload

    with pytest.raises(ValidationError):
        WorkbenchSteerPayload.model_validate({})
    target = str(uuid4())
    assert WorkbenchSteerPayload.model_validate({"target_run_id": target}).target_run_id == target
    assert WorkbenchFollowupsQuery.model_validate({"tracked": ",".join([target] * 4)}).tracked
    with pytest.raises(ValidationError):
        WorkbenchFollowupsQuery.model_validate({"tracked": ",".join([target] * 5)})

    from controllers.console.workbench import WorkbenchRunPayload

    base = {"version": 1, "request_key": "continue", "query": ""}
    with pytest.raises(ValidationError):
        WorkbenchRunPayload.model_validate(base)
    assert WorkbenchRunPayload.model_validate({**base, "continue_run_id": target}).continue_run_id == target


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
        "created_at": 1785542400,
        "updated_at": 1785542400,
        "version": 1,
        "pinned": False,
        "template_snapshot_id": "snapshot",
        "selection": {"model": "provider::model"},
        "runs": [run],
    }
    assert dump_response(WorkbenchChatEnvelopeResponse, {"data": chat})["data"]["runs"][0]["context_usage"] == usage
    run.pop("context_usage")
    assert dump_response(WorkbenchRunEnvelopeResponse, {"data": run})["data"]["context_usage"] is None
