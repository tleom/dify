from types import SimpleNamespace

from services.workbench import catalog_labels


def test_labels_use_chinese_declarations_and_cache_each_provider(monkeypatch):
    calls = []

    def metadata(tenant, kind, provider):
        calls.append((tenant, kind, provider))
        return "时间", {"current_time": {"name": "获取当前时间", "description": "查询时区时间"}}

    monkeypatch.setattr(catalog_labels, "provider_metadata", metadata)
    tools = [
        {"id": "a", "group": "builtin", "provider": "langgenius/time/time", "tool_name": "current_time"},
        {"id": "b", "group": "builtin", "provider": "langgenius/time/time", "tool_name": None},
    ]
    catalog_labels.enrich_tool_labels("tenant", tools)
    assert len(calls) == 1
    assert tools[0]["name"] == "获取当前时间"
    assert tools[1]["name"] == "时间"
    assert tools[0]["provider_name"] == "时间"
    assert tools[0]["id"] == "a" and len(tools) == 2


def test_metadata_failure_does_not_expose_credentials_or_hide_selected_tool(monkeypatch):
    def unavailable(*args):
        raise RuntimeError("private-provider-token")

    monkeypatch.setattr(catalog_labels, "provider_metadata", unavailable)
    tools = [{"id": "a", "group": "mcp", "provider": "server", "name": "read"}]
    catalog_labels.enrich_tool_labels("tenant", tools)
    assert tools == [{"id": "a", "group": "mcp", "provider": "server", "name": "read"}]


def test_localization_fallbacks():
    assert catalog_labels.localized(SimpleNamespace(zh_Hans="中文", en_US="English")) == "中文"
    assert catalog_labels.localized({"en_US": "English"}) == "English"
    assert catalog_labels.localized(None, "fallback") == "fallback"
