from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.workbench import catalog_labels
from services.workbench.mentions import resolve_mentions


def test_chinese_skill_labels_keep_published_execution_ids_and_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    skill = SimpleNamespace(name="browser-control-edited", display_name="浏览器操控")
    version = SimpleNamespace(manifest=SimpleNamespace(name="browser-control", display_name="旧显示名"))
    execute = Mock(return_value=SimpleNamespace(all=lambda: [(skill, version)]))
    monkeypatch.setattr(catalog_labels, "db", SimpleNamespace(session=SimpleNamespace(execute=execute)))
    resources = [
        {"id": "browser-control", "name": "browser-control", "description": "打开网站"},
        {"id": "embedded-skill", "name": "embedded-skill"},
    ]
    catalog_labels.enrich_skill_labels("tenant-a", "agent-a", resources)
    assert resources[0] == {"id": "browser-control", "name": "浏览器操控", "description": "打开网站"}
    assert resources[1]["name"] == "embedded-skill"
    query = str(execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "skills.tenant_id = 'tenant-a'" in query
    assert "agents.tenant_id = 'tenant-a'" in query
    assert "agents.id = 'agent-a'" in query
    assert "agent_skill_binding_snapshots.config_snapshot_id = agents.active_config_snapshot_id" in query


def test_empty_skill_catalog_does_not_query_database(monkeypatch: pytest.MonkeyPatch) -> None:
    execute = Mock(side_effect=AssertionError("No skills to label"))
    monkeypatch.setattr(catalog_labels, "db", SimpleNamespace(session=SimpleNamespace(execute=execute)))
    catalog_labels.enrich_skill_labels("tenant", "agent", [])
    execute.assert_not_called()


def test_saved_badge_is_chinese_while_model_resource_token_uses_skill_identifier() -> None:
    result = resolve_mentions(
        {"config_skills": [{"name": "browser-control"}]},
        {"skills": ["browser-control"]},
        skill_names={"browser-control": "浏览器操控"},
    )
    assert result["mentioned_resources"] == [{"id": "browser-control", "kind": "skills", "name": "浏览器操控"}]
    assert "[§skill:browser-control§]" in result["mention_prompt"]
