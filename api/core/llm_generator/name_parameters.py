"""Keep short conversation names out of a model's default reasoning budget."""

from graphon.model_runtime.entities.model_entities import ParameterType


def conversation_name_parameters(rules):
    parameters: dict[str, int | str | bool] = {"max_tokens": 500, "temperature": 0}
    by_name = {rule.name: rule for rule in rules}
    for key in ("enable_thinking", "thinking"):
        rule = by_name.get(key)
        if rule is None:
            continue
        if rule.type == ParameterType.BOOLEAN:
            parameters[key] = False
        elif "disabled" in rule.options:
            parameters[key] = "disabled"
    effort = by_name.get("reasoning_effort")
    if effort:
        for value in ("none", "minimal", "low"):
            if value in effort.options:
                parameters["reasoning_effort"] = value
                break
    return parameters
