import pytest
from pydantic import ValidationError

from controllers.console.workbench import WorkbenchResumePayload


@pytest.mark.parametrize("payload", [{"values": {"task": "other"}}, {"values": {"task": "other"}, "action": None}])
def test_form_resume_accepts_answers_without_a_separate_action(payload: dict[str, object]) -> None:
    request = WorkbenchResumePayload.model_validate({"request_id": "question-1", **payload})
    assert request.values == {"task": "other"}
    assert request.action is None
    assert "action" not in WorkbenchResumePayload.model_json_schema().get("required", [])


def test_explicit_action_contract_remains_valid() -> None:
    assert WorkbenchResumePayload.model_validate({"action": "review", "request_id": "question-1"}).action == "review"
    for value in ["", "x" * 101]:
        with pytest.raises(ValidationError):
            WorkbenchResumePayload.model_validate({"action": value, "request_id": "question-1"})


def test_resume_requires_the_displayed_request_id() -> None:
    with pytest.raises(ValidationError):
        WorkbenchResumePayload.model_validate({"action": "approve"})
