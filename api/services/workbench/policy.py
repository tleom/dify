"""Public workbench selections; secrets and executable definitions stay in the template."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    tools: list[str] = Field(default_factory=list, max_length=200)
    skills: list[str] = Field(default_factory=list, max_length=200)
    knowledge: list[str] = Field(default_factory=list, max_length=200)
    model_parameters: dict[str, float | int | str | bool] = Field(default_factory=dict)
    tool_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)


def resource_key(value: dict[str, Any]) -> str:
    identity = {k: value.get(k) for k in ("provider_type", "provider_id", "plugin_id", "provider", "tool_name")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def template_resources(soul: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "tools": {resource_key(t): t for t in soul.get("tools", {}).get("dify_tools", []) if t.get("enabled", True)},
        "skills": {s["name"]: s for s in soul.get("config_skills", []) if not s.get("is_missing")},
        "knowledge": {k["id"]: k for k in soul.get("knowledge", {}).get("sets", [])},
    }


def prune_unavailable_selection(soul: dict[str, Any], selection: Selection) -> Selection:
    """Narrow inherited personal defaults to resources still published by the administrator."""
    resources = template_resources(soul)
    clean = selection.model_copy(deep=True)
    for kind in ("tools", "skills", "knowledge"):
        setattr(clean, kind, [key for key in getattr(clean, kind) if key in resources[kind]])
    clean.tool_parameters = {key: value for key, value in clean.tool_parameters.items() if key in clean.tools}
    return clean


def compile_selection(
    soul: dict[str, Any],
    selection: Selection,
    models: dict[str, dict[str, Any]],
    parameter_rules: dict[str, dict[str, Any]] | None = None,
    tool_parameter_rules: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Compile an allowlisted selection without accepting client-supplied credentials or code."""
    resources = template_resources(soul)
    if selection.model not in models:
        raise ValueError("所选模型不可用或未启用工具调用，请重新选择支持工具调用的模型")
    for kind in ("tools", "skills", "knowledge"):
        selected = getattr(selection, kind)
        if len(selected) != len(set(selected)) or not set(selected) <= resources[kind].keys():
            label = {"tools": "工具", "skills": "Skills", "knowledge": "知识库"}[kind]
            raise ValueError(f"所选{label}已失效或未授权，请刷新可用资源并移除失效项")
    result = copy.deepcopy(soul)
    result["model"] = copy.deepcopy(models[selection.model])
    settings = result["model"].setdefault("model_settings", {})
    template_model = soul.get("model") or {}
    if all(template_model.get(key) == result["model"].get(key) for key in ("plugin_id", "model_provider", "model")):
        # A workbench selection overrides individual settings of the published
        # model. Provider-specific settings must not leak into a different model.
        settings.update(copy.deepcopy(template_model.get("model_settings") or {}))
    for key, value in selection.model_parameters.items():
        rule = (parameter_rules or {}).get(key)
        if rule is None:
            raise ValueError(f"模型参数未开放: {key}")
        expected = rule.get("type")
        if (
            expected in ("float", "int")
            and (not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value))
            or expected == "int"
            and int(value) != value
            or expected == "boolean"
            and not isinstance(value, bool)
            or expected in ("string", "text")
            and not isinstance(value, str)
        ):
            raise ValueError(f"模型参数类型无效: {key}")
        if rule.get("options") and value not in rule["options"]:
            raise ValueError(f"模型参数值不在允许范围: {key}")
        if isinstance(value, (float, int)):
            if rule.get("min") is not None and value < rule["min"]:
                raise ValueError(f"模型参数过小: {key}")
            if rule.get("max") is not None and value > rule["max"]:
                raise ValueError(f"模型参数过大: {key}")
        settings[key] = value
    # Executable definitions come only from the administrator's published
    # snapshot, never from a client selection. Preserve their native runtime
    # handling and the sandbox's existing filesystem/execution restrictions.
    result.setdefault("tools", {})["dify_tools"] = []
    if not set(selection.tool_parameters) <= set(selection.tools):
        raise ValueError("不能设置未启用工具的参数")
    for key in selection.tools:
        tool = copy.deepcopy(resources["tools"][key])
        if selection.tool_parameters.get(key):
            # Open only non-secret runtime parameters explicitly declared by the template.
            for name, value in selection.tool_parameters[key].items():
                rule = (tool_parameter_rules or {}).get(key, {}).get(name)
                if rule is None:
                    raise ValueError(f"工具参数未开放: {name}")
                from jsonschema import validate

                validate(value, rule)
                tool.setdefault("runtime_parameters", {})[name] = value
        result["tools"]["dify_tools"].append(tool)
    result["config_skills"] = [copy.deepcopy(resources["skills"][key]) for key in selection.skills]
    result["knowledge"] = {"sets": [copy.deepcopy(resources["knowledge"][key]) for key in selection.knowledge]}
    # Preserve declarations, but do not copy the publisher's inline credentials
    # into an account shell. Reference names are resolved by that account's host.
    environments = [result.get("env") or {}]
    environments.extend(tool.get("env") or {} for tool in result["tools"].get("cli_tools", []))
    for environment in environments:
        for secret_ref in environment.get("secret_refs", []):
            secret_ref.pop("value", None)
    return result


def public_resources(soul: dict[str, Any], tool_parameter_rules: dict | None = None) -> dict[str, list[dict[str, Any]]]:
    resources = template_resources(soul)
    tools = []
    for key, tool in resources["tools"].items():
        parameters = {}
        for name, value in tool.get("runtime_parameters", {}).items():
            rule = (tool_parameter_rules or {}).get(key, {}).get(name)
            if rule is not None:
                parameters[name] = {"value": value, "schema": rule}
        tools.append(
            {
                "id": key,
                "name": tool.get("tool_name") or tool.get("provider") or tool.get("provider_id"),
                "group": tool.get("provider_type"),
                "provider": tool.get("provider_id") or tool.get("provider"),
                "plugin_id": tool.get("plugin_id"),
                "tool_name": tool.get("tool_name"),
                "description": tool.get("description"),
                "parameters": parameters,
            }
        )
    return {
        "tools": tools,
        "skills": [
            {"id": key, "name": value["name"], "description": value.get("description", "")}
            for key, value in resources["skills"].items()
        ],
        "knowledge": [
            {"id": key, "name": value["name"], "description": value.get("description", "")}
            for key, value in resources["knowledge"].items()
        ],
    }
