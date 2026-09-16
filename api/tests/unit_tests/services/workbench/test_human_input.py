"""Form answers resume without inventing a separate human-selected action."""

import json
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import JsonValue
from werkzeug.exceptions import Conflict, NotFound

from services.workbench import scheduler, service


@dataclass
class PendingRun:
    run: SimpleNamespace
    args: dict[str, JsonValue]
    session: Mock
    authorize: Mock
    publish: Mock

    @property
    def request_id(self) -> str:
        payload = json.loads(self.run.payload)
        return service.input_request_id(self.run, payload) or payload["submitted_input"]["request_id"]


@pytest.fixture
def pending_run(monkeypatch: pytest.MonkeyPatch) -> PendingRun:
    args: dict[str, JsonValue] = {
        "question": "如何处理材料？",
        "fields": [
            {
                "type": "select",
                "name": "task",
                "label": "任务",
                "required": True,
                "options": [{"value": "polish", "label": "润色"}, {"value": "other", "label": "其他"}],
            },
            {"type": "paragraph", "name": "extra", "label": "补充说明"},
        ],
        "actions": [{"id": "polish", "label": "润色排版"}, {"id": "review", "label": "深度审查"}],
    }
    run = SimpleNamespace(
        id="run",
        status="waiting_input",
        backend_run_id="backend",
        payload=json.dumps({"pending": {"tool_call_id": "question", "args": args}}),
    )
    session = Mock()
    session.scalar.return_value = run
    monkeypatch.setattr(
        service.session_factory, "get_session_maker", lambda: SimpleNamespace(begin=lambda: nullcontext(session))
    )
    authorize = Mock()
    monkeypatch.setattr(service, "authorize", authorize)
    monkeypatch.setattr(service, "run_dto", lambda item: {"id": item.id, "status": item.status})
    publish = Mock()
    monkeypatch.setattr(scheduler, "publish", publish)
    return PendingRun(run=run, args=args, session=session, authorize=authorize, publish=publish)


def test_form_submission_preserves_answers_without_selecting_the_first_action(pending_run: PendingRun) -> None:
    values = {"task": "other", "extra": "只核对引用"}
    request_id = pending_run.request_id
    assert service.resume("tenant", "account", "run", values, None, request_id) == {"id": "run", "status": "queued"}
    payload = json.loads(pending_run.run.payload)
    result = payload["continuation"]["calls"]["question"]
    assert result["status"] == "submitted"
    assert result["values"] == values
    assert result["action"] is None
    assert "pending" not in payload
    assert payload["submitted_input"] == {"values": values, "action": None, "request_id": request_id}
    pending_run.authorize.assert_called_once_with("tenant", "account")
    query = pending_run.session.scalar.call_args.args[0].compile()
    assert query.params == {"id_1": "run", "tenant_id_1": "tenant", "account_id_1": "account"}
    pending_run.publish.assert_called_once_with("tenant", "account", "run")
    assert service.resume("tenant", "account", "run", values, None, request_id)["status"] == "queued"
    pending_run.publish.assert_called_once()
    with pytest.raises(Conflict):
        service.resume("tenant", "account", "run", {"task": "polish"}, None, request_id)


def test_legacy_explicit_action_remains_supported(pending_run: PendingRun) -> None:
    service.resume("tenant", "account", "run", {"task": "other"}, "review", pending_run.request_id)
    result = json.loads(pending_run.run.payload)["continuation"]["calls"]["question"]
    assert result["action"] == {"id": "review", "label": "深度审查"}


@pytest.mark.parametrize("action", [None, "missing"])
def test_action_only_question_requires_a_valid_selection(pending_run: PendingRun, action: str | None) -> None:
    pending_run.args["fields"] = list[JsonValue]()
    pending_run.run.payload = json.dumps({"pending": {"tool_call_id": "question", "args": pending_run.args}})
    with pytest.raises(ValueError, match="请选择操作|操作选项无效"):
        service.resume("tenant", "account", "run", {}, action, pending_run.request_id)
    pending_run.publish.assert_not_called()
    assert pending_run.run.status == "waiting_input"


def test_action_only_selection_is_returned_to_the_agent(pending_run: PendingRun) -> None:
    pending_run.args["fields"] = list[JsonValue]()
    pending_run.run.payload = json.dumps({"pending": {"tool_call_id": "question", "args": pending_run.args}})
    service.resume("tenant", "account", "run", {}, "review", pending_run.request_id)
    result = json.loads(pending_run.run.payload)["continuation"]["calls"]["question"]
    assert result["action"] == {"id": "review", "label": "深度审查"}


@pytest.mark.parametrize(
    ("values", "action", "message"),
    [
        ({}, None, "请填写 任务"),
        ({"task": "missing"}, None, "任务 选项无效"),
        ({"task": "other", "unknown": "value"}, None, "输入包含未请求的字段"),
        ({"task": "other"}, "missing", "操作选项无效"),
    ],
)
def test_invalid_answers_and_actions_still_fail(
    pending_run: PendingRun, values: dict[str, str], action: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        service.resume("tenant", "account", "run", values, action, pending_run.request_id)
    pending_run.publish.assert_not_called()
    assert pending_run.run.status == "waiting_input"


@pytest.mark.parametrize("change", ["question", "attempt", "backend", "missing"])
def test_obsolete_or_missing_request_cannot_answer_a_later_question(pending_run: PendingRun, change: str) -> None:
    original = pending_run.request_id
    payload = json.loads(pending_run.run.payload)
    if change == "question":
        payload["pending"]["args"]["question"] = "新的问题"
    elif change == "attempt":
        payload["attempt"] = 2
    elif change == "backend":
        pending_run.run.backend_run_id = "later-execution"
    pending_run.run.payload = json.dumps(payload)
    with pytest.raises(Conflict, match="输入请求已更新"):
        service.resume(
            "tenant", "account", "run", {"task": "other"}, "review", None if change == "missing" else original
        )
    assert pending_run.run.status == "waiting_input"
    assert json.loads(pending_run.run.payload) == payload
    pending_run.publish.assert_not_called()


def test_resume_does_not_accept_a_run_outside_the_owner(pending_run: PendingRun) -> None:
    pending_run.session.scalar.return_value = None
    with pytest.raises(NotFound):
        service.resume("tenant", "foreign", "run", {"task": "other"}, None)
    pending_run.publish.assert_not_called()


def test_partial_answer_preserves_values_and_explicit_skips(pending_run: PendingRun) -> None:
    request_id = pending_run.request_id
    service.resume("tenant", "account", "run", {"extra": "只核对引用"}, None, request_id, skipped_fields=["task"])
    result = json.loads(pending_run.run.payload)["continuation"]["calls"]["question"]
    assert result["values"] == {"extra": "只核对引用"}
    assert result["skipped_fields"] == ["task"]
    assert (
        service.resume("tenant", "account", "run", {"extra": "只核对引用"}, None, request_id, skipped_fields=["task"])[
            "status"
        ]
        == "queued"
    )
    pending_run.publish.assert_called_once()


def test_custom_choice_requires_explicit_marker(pending_run: PendingRun) -> None:
    service.resume(
        "tenant", "account", "run", {"task": "只校对英文"}, None, pending_run.request_id, custom_fields=["task"]
    )
    result = json.loads(pending_run.run.payload)["continuation"]["calls"]["question"]
    assert result["custom_fields"] == ["task"]
    assert result["values"]["task"] == "只校对英文"


@pytest.mark.parametrize(
    ("values", "details"),
    [
        ({"task": "other"}, {"skipped_fields": ["task"]}),
        ({}, {"skipped_fields": ["invented"]}),
        ({"task": "other", "extra": "文字"}, {"custom_fields": ["extra"]}),
        ({"task": " "}, {"custom_fields": ["task"]}),
    ],
)
def test_invalid_answer_details_do_not_resume(pending_run: PendingRun, values, details) -> None:
    with pytest.raises(ValueError):
        service.resume("tenant", "account", "run", values, None, pending_run.request_id, **details)
    pending_run.publish.assert_not_called()


@pytest.mark.parametrize("details", [{"custom_fields": ["task"]}, {"skipped_fields": ["task"]}])
def test_plan_cannot_be_approved_through_question_shortcuts(pending_run: PendingRun, details) -> None:
    payload = json.loads(pending_run.run.payload)
    payload["pending"]["tool_name"] = "exit_plan_mode"
    pending_run.run.payload = json.dumps(payload)
    with pytest.raises(ValueError, match="计划审核必须明确选择"):
        service.resume("tenant", "account", "run", {}, "approve", pending_run.request_id, **details)
    pending_run.publish.assert_not_called()
