from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.workbench import mentions, resources, runtime, service


def test_personal_catalog_uses_owner_and_excludes_disabled_or_invalid_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    ensure = Mock(return_value="owner-workspace")
    manager = Mock(
        return_value={
            "skills": [
                {"id": "write", "enabled": True, "content": "---\nname: write\ndescription: 我的写作\n---\nFollow me"},
                {"id": "disabled", "enabled": False, "content": "---\nname: disabled\ndescription: disabled\n---"},
                {"id": "broken", "enabled": True, "content": "missing metadata"},
            ]
        }
    )
    monkeypatch.setattr(resources, "ensure_workspace", ensure)
    monkeypatch.setattr(resources, "manager", manager)
    assert resources.personal_skill_catalog("tenant-a", "account-b") == [
        {"id": "personal:write", "name": "write", "description": "我的写作", "scope": "personal"},
    ]
    ensure.assert_called_once_with("tenant-a", "account-b")
    manager.assert_called_once_with("owner-workspace", "personal-resources", {"operation": "list"})


def test_catalog_places_personal_skills_after_global_without_default_config_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "template", Mock(return_value={"soul": {}, "agent_id": "agent"}))
    monkeypatch.setattr(service, "models_and_rules", Mock(return_value={}))
    monkeypatch.setattr(
        service,
        "public_resources",
        Mock(return_value={"skills": [{"id": "write", "name": "写作"}], "tools": [], "knowledge": []}),
    )
    monkeypatch.setattr(service, "enrich_tool_labels", Mock())
    monkeypatch.setattr(service, "enrich_skill_labels", Mock())
    monkeypatch.setattr(
        service, "default_selection", Mock(return_value=SimpleNamespace(model_dump=lambda **_: {"skills": ["write"]}))
    )
    personal = Mock(return_value=[{"id": "personal:write", "name": "write", "scope": "personal"}])
    monkeypatch.setattr(resources, "personal_skill_catalog", personal)
    result = service.catalog("tenant", "owner")
    assert [item["id"] for item in result["skills"]] == ["write", "personal:write"]
    assert result["skills"][0]["scope"] == "global"
    assert result["default_selection"]["skills"] == ["write"]
    personal.assert_called_once_with("tenant", "owner")


def test_personal_mentions_are_scoped_and_do_not_override_same_name_global_skill() -> None:
    soul = {"config_skills": [{"name": "write"}]}
    personal = [{"id": "personal:write", "name": "write"}]
    result = mentions.resolve_mentions(soul, {"skills": ["write", "personal:write"]}, personal_skills=personal)
    assert [item["id"] for item in result["mentioned_resources"]] == ["write", "personal:write"]
    assert "[§skill:write§]" in result["mention_prompt"]
    assert '"scope": "personal"' in result["mention_prompt"]
    assert '"resource": "read_skill"' in result["mention_prompt"]
    with pytest.raises(ValueError, match="个人技能已不可用"):
        mentions.resolve_mentions(soul, {"skills": ["personal:write"]}, personal_skills=[])
    with pytest.raises(ValueError, match="个人技能已不可用"):
        mentions.resolve_mentions(soul, {"skills": ["personal:other-account"]}, personal_skills=personal)


@pytest.mark.parametrize("available", [True, False])
def test_runtime_revalidates_personal_owner_but_materializes_only_global_archives(
    monkeypatch: pytest.MonkeyPatch, available: bool
) -> None:
    monkeypatch.setattr(
        mentions, "load_run_mentions", Mock(return_value=mentions.ResourceMentions(skills=["write", "personal:write"]))
    )
    listing = Mock(return_value=[{"id": "personal:write"}] if available else [])
    monkeypatch.setattr(resources, "personal_skill_catalog", listing)
    groups = Mock(return_value=[])
    monkeypatch.setattr(mentions, "required_tool_groups", groups)
    soul = SimpleNamespace(model_dump=lambda **_: {})
    if available:
        assert runtime.resolve_run_requirements("run", "tenant", "owner", soul, object()) == (["write"], [])
        assert groups.call_args.args[1].skills == ["write"]
    else:
        with pytest.raises(ValueError, match="个人技能已失效"):
            runtime.resolve_run_requirements("run", "tenant", "owner", soul, object())
        groups.assert_not_called()
    listing.assert_called_once_with("tenant", "owner")
