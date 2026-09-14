import pytest
from pydantic import ValidationError

from controllers.console.workbench import WorkbenchResumePayload


@pytest.mark.parametrize("payload", [{"values": {"task": "other"}}, {"values": {"task": "other"}, "action": None}])
def test_form_resume_accepts_answers_without_a_separate_action(payload) -> None:
    request = WorkbenchResumePayload.model_validate(payload)
    assert request.values == {"task": "other"}
    assert request.action is None
    assert "action" not in WorkbenchResumePayload.model_json_schema().get("required", [])


def test_explicit_action_contract_remains_valid() -> None:
    assert WorkbenchResumePayload.model_validate({"action": "review"}).action == "review"
    for value in ["", "x" * 101]:
        with pytest.raises(ValidationError):
            WorkbenchResumePayload.model_validate({"action": value})
