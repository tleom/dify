from types import SimpleNamespace

from core.llm_generator.name_parameters import conversation_name_parameters
from graphon.model_runtime.entities.model_entities import ParameterType


def test_disables_qwen_default_thinking_for_short_title() -> None:
    rules = [
        SimpleNamespace(name="enable_thinking", type=ParameterType.BOOLEAN, options=[]),
        SimpleNamespace(name="reasoning_effort", type=ParameterType.STRING, options=["low", "medium", "xhigh"]),
    ]
    assert conversation_name_parameters(rules) == {
        "max_tokens": 500,
        "temperature": 0,
        "enable_thinking": False,
        "reasoning_effort": "low",
    }


def test_uses_declared_disabled_option_and_no_unknown_fields() -> None:
    rules = [SimpleNamespace(name="thinking", type=ParameterType.STRING, options=["enabled", "disabled"])]
    assert conversation_name_parameters(rules)["thinking"] == "disabled"
    assert conversation_name_parameters([]) == {"max_tokens": 500, "temperature": 0}
