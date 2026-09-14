"""Verify references and frozen turn requirements across the native request builder."""

from dataclasses import replace

import pytest

from core.app.apps.agent_app.runtime_request_builder import AgentAppRuntimeRequestBuilder
from models.agent_config_entities import AgentSoulConfig
from services.workbench.mentions import ResourceMentions
from services.workbench.policy import resource_key
from tests.unit_tests.core.app.apps.agent_app.test_runtime_request_builder import (
    _ctx,
    _PluginLayerBuilder,
    _soul_with_model_and_skill,
)


def test_user_only_skill_and_plugin_mentions_are_required_only_for_this_turn(monkeypatch):
    data = _soul_with_model_and_skill().model_dump(mode="json")
    data["prompt"]["system_prompt"] = "Answer the request."
    data["tools"]["dify_tools"] = [
        {
            "provider_type": "plugin",
            "plugin_id": "langgenius/time",
            "provider": "time",
            "tool_name": "current_time",
            "credential_type": "unauthorized",
        }
    ]
    soul = AgentSoulConfig.model_validate(data)
    key = resource_key(soul.tools.dify_tools[0].model_dump(mode="json"))
    mentions = ResourceMentions(tools=[key], skills=["tender-analyzer"])
    monkeypatch.setattr(
        "core.app.apps.agent_app.runtime_request_builder.resolve_model_context_window", lambda **_kwargs: 8192
    )
    monkeypatch.setattr("core.app.apps.agent_app.runtime_request_builder.load_run_mentions", lambda *_args: mentions)
    builder = AgentAppRuntimeRequestBuilder(dify_tools_builder=_PluginLayerBuilder())
    first = builder.build(replace(_ctx(soul), workbench_run_id="first-run")).request
    config = next(layer.config for layer in first.composition.layers if layer.name == "config")
    assert config.mentioned_skill_names == ["tender-analyzer"]
    required = next(layer.config for layer in first.composition.layers if layer.name == "workbench_mentions")
    assert required.workbench_run_id == "first-run"
    assert required.tool_groups[0].tool_names == ["current_time"]
    mentions = ResourceMentions()
    second = builder.build(replace(_ctx(soul), workbench_run_id="second-run")).request
    config = next(layer.config for layer in second.composition.layers if layer.name == "config")
    assert config.mentioned_skill_names == []
    assert all(layer.name != "workbench_mentions" for layer in second.composition.layers)
    assert soul.prompt.system_prompt == "Answer the request."


@pytest.mark.parametrize("kind", ["provider", "model"])
def test_model_reference_reaches_runtime_and_context_capability_resolution(monkeypatch, kind):
    data = _soul_with_model_and_skill().model_dump(mode="json")
    reference = {"type": kind, "id": "explicit-credential", "provider": "langgenius/openai/openai"}
    data["model"]["credential_ref"] = reference
    seen = []
    monkeypatch.setattr(
        "core.app.apps.agent_app.runtime_request_builder.load_runtime_agent_skill_configs", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        "core.app.apps.agent_app.runtime_request_builder.resolve_model_context_window",
        lambda **kwargs: seen.append(kwargs) or 16384,
    )
    built = AgentAppRuntimeRequestBuilder(dify_tools_builder=_PluginLayerBuilder()).build(
        _ctx(AgentSoulConfig.model_validate(data))
    )
    llm = next(layer.config for layer in built.request.composition.layers if layer.name == "llm")
    assert llm.credential_ref.model_dump() == reference
    assert seen[0]["credential_ref"] == reference
    assert llm.context_window_tokens == 16384
