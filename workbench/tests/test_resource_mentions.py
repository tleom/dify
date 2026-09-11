import pytest
from services.workbench.mentions import default_capabilities, resolve_mentions
from services.workbench.policy import Selection, resource_key


def fixtures():
    tools = [{'provider_type': 'plugin', 'plugin_id': 'acme/tools', 'provider': 'acme', 'tool_name': name} for name in ['first', 'second']]
    soul = {'tools': {'dify_tools': tools}, 'config_skills': [{'name': 'writing'}],
            'knowledge': {'sets': [{'id': 'library', 'name': '资料库'}]}}
    return soul, [resource_key(tool) for tool in tools]


def test_defaults_enable_every_skill_and_tool_but_start_new_chat_without_knowledge():
    soul, ids = fixtures()
    chosen = Selection(model='provider::model', knowledge=['library'])
    new = default_capabilities(soul, chosen, new_chat=True)
    assert new.tools == ids and new.skills == ['writing'] and new.knowledge == []
    assert default_capabilities(soul, chosen).knowledge == ['library']


def test_plugin_tools_share_one_badge_and_native_mentions_keep_individual_calls():
    soul, ids = fixtures()
    result = resolve_mentions(soul, {'tools': ids, 'skills': ['writing'], 'knowledge': ['library']})
    assert len(result['mentioned_resources']) == 3
    assert '[§tool:first§]' in result['mention_prompt'] and '[§tool:second§]' in result['mention_prompt']
    assert '[§skill:writing§]' in result['mention_prompt'] and '[§knowledge:library§]' in result['mention_prompt']
    assert resolve_mentions(soul, {})['mention_prompt'] == ''
    with pytest.raises(ValueError):
        resolve_mentions(soul, {'knowledge': ['another-tenant-library']})


def test_plugin_mention_preserves_the_localized_provider_label():
    soul, ids = fixtures()
    result = resolve_mentions(soul, {"tools": ids}, provider_names=dict.fromkeys(ids, "工具插件"))
    assert result["mentioned_resources"][0]["name"] == "工具插件"
