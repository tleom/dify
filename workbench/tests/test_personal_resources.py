"""Real POSIX filesystem tests; run with pytest on Linux."""

import base64
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires POSIX descriptor operations")


@pytest.fixture
def resources():
    base = Path(__file__).parents[1] / "sandbox-manager"
    for name in ("file_ops", "resource_ops"):
        spec = importlib.util.spec_from_file_location(name, base / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules['resource_ops']


def package(text="---\nname: report\ndescription: Verify reports\n---\n# Report skill"):
    return {"files": [{"path": "SKILL.md", "data": base64.b64encode(text.encode()).decode()}]}


def test_memory_is_per_workspace_and_uses_version_conflicts(resources, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    first = resources.personal({"operation": "memory_update", "content": "用户甲偏好中文", "version": None}, str(a))
    assert resources.personal({"operation": "list"}, str(b))["memory"]["content"] == ""
    assert resources.personal({"operation": "memory_update", "content": "overwritten", "version": None}, str(a)) == {"conflict": True}
    assert resources.personal({"operation": "list"}, str(a))["memory"] == first


def test_import_toggle_replace_and_recoverable_backup(resources, tmp_path):
    first = resources.personal({"operation": "skill_import", "name": "report", **package()}, str(tmp_path))
    resources.personal({"operation": "skill_toggle", "name": "report", "enabled": False}, str(tmp_path))
    assert resources.personal({"operation": "list"}, str(tmp_path))["skills"][0]["enabled"] is False
    assert resources.personal({"operation": "skill_import", "name": "report", **package()}, str(tmp_path)) == {"conflict": True}
    resources.personal({"operation": "skill_import", "name": "report", "version": first["version"], **package("updated")}, str(tmp_path))
    assert (tmp_path / "skills/report/SKILL.md").read_text() == "updated"
    assert next((tmp_path / ".skill-backups").iterdir()).joinpath("SKILL.md").read_text().startswith("---")


@pytest.mark.parametrize("old", [b"a" * (64 * 1024 + 1), b"\xff\xfe"])
def test_invalid_memory_can_be_replaced_without_blocking_skills(resources, tmp_path, old):
    (tmp_path / "memory.md").write_bytes(old)
    resources.personal({"operation": "skill_import", "name": "report", **package()}, str(tmp_path))
    resources.personal({"operation": "skill_toggle", "name": "report", "enabled": False}, str(tmp_path))
    listing = resources.personal({"operation": "list"}, str(tmp_path))
    assert listing["warnings"] and not listing["skills"][0]["enabled"]
    assert resources.personal({"operation": "memory_update", "content": "short", "version": None}, str(tmp_path)) == {"conflict": True}
    resources.personal({"operation": "memory_update", "content": "新记忆", "version": listing["memory"]["version"]}, str(tmp_path))
    assert (tmp_path / "memory.md").read_text(encoding="utf-8") == "新记忆"


def test_unreadable_memory_does_not_block_independent_skill_operations(resources, tmp_path):
    (tmp_path / "memory.md").symlink_to(tmp_path / "missing")
    resources.personal({"operation": "skill_import", "name": "report", **package()}, str(tmp_path))
    resources.personal({"operation": "skill_toggle", "name": "report", "enabled": False}, str(tmp_path))
    assert (tmp_path / "memory.md").is_symlink()


def test_skills_after_the_previous_listing_limit_remain_visible(resources, tmp_path):
    for index in range(105):
        directory = tmp_path / "skills" / f"skill-{index:03}"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"# Skill {index}", encoding="utf-8")
    result = resources.personal({"operation": "list"}, str(tmp_path))
    assert len(result["skills"]) == 105
    assert result["skills"][-1]["id"] == "skill-104"


@pytest.mark.parametrize("bad", ["../escape", "/outside", "a/../../escape", "a\\escape", "C:/escape", "a//escape"])
def test_archive_path_traversal_rejected_before_write(resources, bad):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("SKILL.md", "skill")
        archive.writestr(bad, "bad")
    with pytest.raises(ValueError):
        resources.members({"archive": base64.b64encode(stream.getvalue()).decode()})


def test_zip_symlinks_and_live_symlinks_do_not_escape(resources, tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        info = zipfile.ZipInfo("SKILL.md")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError):
        resources.members({"archive": base64.b64encode(stream.getvalue()).decode()})
    (tmp_path / "memory.md").symlink_to("/etc/passwd")
    listing = resources.personal({"operation": "list"}, str(tmp_path))
    assert listing["memory"]["content"] == "" and listing["warnings"]
    with pytest.raises(OSError):
        resources.personal({"operation": "memory_update", "content": "bad"}, str(tmp_path))


def test_invalid_skill_does_not_break_memory_or_other_skills(resources, tmp_path):
    resources.personal({"operation": "skill_import", "name": "report", **package()}, str(tmp_path))
    broken = tmp_path / "skills/broken"
    broken.mkdir()
    (broken / "SKILL.md").write_bytes(b"\xff\xfe")
    result = resources.personal({"operation": "list"}, str(tmp_path))
    assert len(result["skills"]) == 1 and result["warnings"]


def test_global_package_is_read_only_for_the_sandbox_user(resources, tmp_path):
    root = tmp_path / "global"
    result = resources.global_install({"packages": [{"name": "report", **package()}]}, str(root))
    skill = Path(result["skills"][0]["path"]) / "SKILL.md"
    assert stat.S_IMODE(skill.stat().st_mode) == 0o444
    assert stat.S_IMODE(skill.parent.stat().st_mode) == 0o555
    if os.getuid() == 0:
        for parent in [tmp_path, *tmp_path.parents]:
            if str(parent) == "/tmp":
                break
            parent.chmod(0o755)
        def become_user():
            os.setgid(1000)
            os.setuid(1000)
        check = subprocess.run([sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('bad')", str(skill)], preexec_fn=become_user, capture_output=True)
        assert check.returncode != 0 and b"PermissionError" in check.stderr
    assert skill.read_text().startswith("---")


def test_pin_is_persistent_and_isolated_without_changing_enable_state(resources, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    for root in (a, b):
        resources.personal({"operation": "skill_import", "name": "report", **package()}, str(root))
    resources.personal({"operation": "skill_toggle", "name": "report", "enabled": False}, str(a))
    resources.personal({"operation": "skill_pin", "name": "report", "pinned": True}, str(a))
    first = resources.personal({"operation": "list"}, str(a))["skills"][0]
    other = resources.personal({"operation": "list"}, str(b))["skills"][0]
    assert first["pinned"] is True and first["enabled"] is False
    assert other["pinned"] is False and other["enabled"] is True


def test_edit_preserves_assets_and_keeps_source_backup_with_version_checks(resources, tmp_path):
    payload = package()
    payload["files"].append({"path": "scripts/helper.py", "data": base64.b64encode(b"original asset").decode()})
    first = resources.personal({"operation": "skill_import", "name": "report", **payload}, str(tmp_path))
    result = resources.personal({"operation": "skill_update", "name": "report", "version": first["version"], "content": "updated"}, str(tmp_path))
    assert result["version"] != first["version"]
    assert (tmp_path / "skills/report/SKILL.md").read_text() == "updated"
    assert (tmp_path / "skills/report/scripts/helper.py").read_text() == "original asset"
    assert next((tmp_path / ".skill-backups").iterdir()).joinpath("SKILL.md").read_text().startswith("---")
    assert resources.personal({"operation": "skill_update", "name": "report", "version": first["version"], "content": "stale"}, str(tmp_path)) == {"conflict": True}
    assert (tmp_path / "skills/report/SKILL.md").read_text() == "updated"


def test_uninstall_requires_version_and_moves_the_whole_package_to_backup(resources, tmp_path):
    first = resources.personal({"operation": "skill_import", "name": "report", **package()}, str(tmp_path))
    assert resources.personal({"operation": "skill_uninstall", "name": "report", "version": "stale"}, str(tmp_path)) == {"conflict": True}
    result = resources.personal({"operation": "skill_uninstall", "name": "report", "version": first["version"]}, str(tmp_path))
    assert result == {"id": "report", "uninstalled": True}
    assert resources.personal({"operation": "list"}, str(tmp_path))["skills"] == []
    assert next((tmp_path / ".skill-backups").iterdir()).joinpath("SKILL.md").read_text().startswith("---")


@pytest.mark.parametrize("operation", ["skill_pin", "skill_update", "skill_uninstall"])
def test_new_skill_operations_reject_symlink_targets(resources, tmp_path, operation):
    (tmp_path / "skills").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("untouched")
    (tmp_path / "skills/report").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        resources.personal({"operation": operation, "name": "report", "pinned": True, "version": "stale", "content": "bad"}, str(tmp_path))
    assert (outside / "SKILL.md").read_text() == "untouched"
