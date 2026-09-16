"""Personal MCP filesystem contract, using real POSIX files and owner roots."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires POSIX descriptor operations")


@pytest.fixture
def mcp_ops():
    base = Path(__file__).parents[1] / "sandbox-manager"
    for name in ("file_ops", "resource_ops", "mcp_ops"):
        spec = importlib.util.spec_from_file_location(name, base / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules["mcp_ops"]


def config(**overrides):
    return json.dumps(
        {
            "name": "个人工具",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer private-token"},
            **overrides,
        }
    )


def save(module, root, name="demo", content=None, revision=None):
    return module.personal_mcp(
        {"operation": "mcp_save", "name": name, "content": content or config(), "version": revision}, str(root)
    )


def test_config_is_owned_file_and_listing_excludes_credentials(mcp_ops, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    saved = save(mcp_ops, a)
    assert (a / "mcp/demo/mcp.json").read_text() == config()
    assert mcp_ops.personal_mcp({"operation": "mcp_list"}, str(b))["mcp"] == []
    listing = mcp_ops.personal_mcp({"operation": "mcp_list"}, str(a))
    assert listing["mcp"][0]["version"] == saved["version"]
    assert "private-token" not in json.dumps(listing)
    assert "headers" not in json.dumps(listing)
    assert "private-token" in mcp_ops.personal_mcp({"operation": "mcp_read", "name": "demo"}, str(a))["content"]


def test_version_checks_backups_and_delete_invalidate_cached_tools(mcp_ops, tmp_path):
    first = save(mcp_ops, tmp_path)
    assert save(mcp_ops, tmp_path, content=config(name="冲突")) == {"conflict": True}
    second = save(mcp_ops, tmp_path, content=config(name="新名称"), revision=first["version"])
    assert any("private-token" in path.read_text() for path in (tmp_path / ".mcp-backups").iterdir())
    assert mcp_ops.personal_mcp(
        {"operation": "mcp_delete", "name": "demo", "version": first["version"]}, str(tmp_path)
    ) == {"conflict": True}
    mcp_ops.personal_mcp(
        {"operation": "mcp_cache", "name": "demo", "version": second["version"], "tools": []}, str(tmp_path)
    )
    mcp_ops.personal_mcp({"operation": "mcp_toggle", "name": "demo", "enabled": False}, str(tmp_path))
    mcp_ops.personal_mcp({"operation": "mcp_delete", "name": "demo", "version": second["version"]}, str(tmp_path))
    assert not (tmp_path / "mcp/demo").exists()
    assert not (tmp_path / ".mcp-cache/demo.json").exists()
    save(mcp_ops, tmp_path, content=config(name="新名称"))
    item = mcp_ops.personal_mcp({"operation": "mcp_list"}, str(tmp_path))["mcp"][0]
    assert item["status"] == "unverified" and item["enabled"]


def test_cache_and_direct_file_edit_follow_config_version(mcp_ops, tmp_path):
    first = save(mcp_ops, tmp_path)
    tools = [{"name": "query", "description": "查询", "inputSchema": {"type": "object", "properties": {}}}]
    mcp_ops.personal_mcp(
        {"operation": "mcp_cache", "name": "demo", "version": first["version"], "tools": tools}, str(tmp_path)
    )
    assert mcp_ops.personal_mcp({"operation": "mcp_list"}, str(tmp_path))["mcp"][0]["tools"] == tools
    (tmp_path / "mcp/demo/mcp.json").write_text(config(name="直接修改"))
    item = mcp_ops.personal_mcp({"operation": "mcp_list"}, str(tmp_path))["mcp"][0]
    assert item["name"] == "直接修改" and item["tools"] == [] and item["status"] == "unverified"


def test_toggle_pin_and_corrupt_config_do_not_affect_other_owners(mcp_ops, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for root in (a, b):
        save(mcp_ops, root)
    mcp_ops.personal_mcp({"operation": "mcp_toggle", "name": "demo", "enabled": False}, str(a))
    mcp_ops.personal_mcp({"operation": "mcp_pin", "name": "demo", "pinned": True}, str(a))
    assert not mcp_ops.personal_mcp({"operation": "mcp_list"}, str(a))["mcp"][0]["enabled"]
    assert mcp_ops.personal_mcp({"operation": "mcp_list"}, str(b))["mcp"][0]["enabled"]
    (a / "mcp/demo/mcp.json").write_bytes(b"not json")
    bad = mcp_ops.personal_mcp({"operation": "mcp_list"}, str(a))["mcp"][0]
    assert bad["status"] == "invalid" and bad["version"]
    assert mcp_ops.personal_mcp({"operation": "mcp_delete", "name": "demo", "version": bad["version"]}, str(a))[
        "removed"
    ]


@pytest.mark.parametrize("name", ["../escape", "/tmp/outside", "UPPER", "a/b", "a\\b"])
def test_invalid_paths_never_write(mcp_ops, tmp_path, name):
    with pytest.raises(ValueError):
        save(mcp_ops, tmp_path, name=name)
    assert not list(tmp_path.iterdir())


def test_symlinks_and_invalid_settings_fail_closed(mcp_ops, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp/demo").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        save(mcp_ops, tmp_path)
    assert not list(outside.iterdir())
    save(mcp_ops, tmp_path, name="valid")
    (tmp_path / ".mcp-settings.json").write_text("[]")
    result = mcp_ops.personal_mcp({"operation": "mcp_list"}, str(tmp_path))
    assert result["warnings"] and not any(item["enabled"] for item in result["mcp"])


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        "{}",
        '{"command": "python", "args": "wrong"}',
        '{"url": "file:///etc/passwd"}',
        '{"url": "https://name:secret@example.com"}',
    ],
)
def test_invalid_config_does_not_create_an_active_entry(mcp_ops, tmp_path, content):
    with pytest.raises(ValueError):
        save(mcp_ops, tmp_path, content=content)
    assert not (tmp_path / "mcp/demo").exists()


def test_external_schema_references_are_not_fetched(mcp_ops):
    with pytest.raises(ValueError, match="外部"):
        mcp_ops.tool_manifest(
            [
                {
                    "name": "bad",
                    "inputSchema": {"type": "object", "properties": {"x": {"$ref": "http://internal/schema"}}},
                }
            ]
        )
