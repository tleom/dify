"""Management command isolation and ordinary helper I/O, without startup payloads."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
from types import SimpleNamespace

import pytest


@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKBENCH_SANDBOX_MANAGER_TOKEN", "test-token-" * 4)
    monkeypatch.setenv("WORKBENCH_MANAGER_STATE", str(tmp_path / "state"))
    spec = importlib.util.spec_from_file_location("isolated_manager", Path(__file__).parents[1] / "sandbox-manager/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ensure", lambda key: {})
    return module


@pytest.mark.parametrize("action,operation,user", [("files", "list", "1000"), ("personal-resources", "list", "1000"), ("global-resources", "global_install", "0")])
def test_helpers_use_trusted_isolated_python_and_preserve_transport(manager, action, operation, user):
    calls = []

    def docker(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok": true}')

    manager.docker = docker
    payload = {"operation": operation, "path": ".", "packages": []}
    result = manager.operation("12345678-1234-5678-1234-567812345678", action, payload)
    args, options = calls[0]
    assert args[1:4] == ("--user", user, "-i")
    assert args[5:9] == ("/usr/local/bin/python", "-I", "-S", "-c")
    assert json.loads(options["stdin"]) == payload and options["check"] is False
    assert result == {"ok": True}


@pytest.mark.skipif(os.name != "posix", reason="Production Python path and descriptor operations require Linux")
def test_real_isolated_interpreter_and_injected_helpers(manager, tmp_path):
    cwd, pythonpath, workspace, global_root = [tmp_path / name for name in ("cwd", "pythonpath", "workspace", "global")]
    for directory in (cwd, pythonpath, workspace):
        directory.mkdir()
    env = {**os.environ, "PYTHONPATH": str(pythonpath), "PYTHONUSERBASE": str(tmp_path / "userbase")}
    flags = subprocess.run([*manager.MANAGER_PYTHON, "-c", "import json,sys; print(json.dumps({'isolated':sys.flags.isolated,'no_site':sys.flags.no_site,'path':sys.path,'executable':sys.executable}))"], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    observed = json.loads(flags.stdout)
    assert observed["isolated"] == observed["no_site"] == 1
    assert observed["executable"] == manager.MANAGER_PYTHON[0]
    assert str(cwd) not in observed["path"] and str(pythonpath) not in observed["path"] and "" not in observed["path"]

    def docker(*args, **kwargs):
        # Execute the exact manager prefix and source in an ordinary temporary
        # workspace; only the helper's destination roots differ from deployment.
        prefix = args[5:9]
        assert prefix == (*manager.MANAGER_PYTHON, "-c")
        source = args[-1].replace('/workspace', str(workspace)).replace('/opt/workbench-global', str(global_root))
        return subprocess.run([*prefix, source], input=kwargs["stdin"], cwd=cwd, env=env, capture_output=True, text=True)

    manager.docker = docker
    owner = "12345678-1234-5678-1234-567812345678"
    memory = manager.operation(owner, "personal-resources", {"operation": "memory_update", "content": "ordinary memory", "version": None})
    listing = manager.operation(owner, "personal-resources", {"operation": "list"})
    assert listing["memory"] == memory
    package = {"name": "report", "files": [{"path": "SKILL.md", "data": base64.b64encode(b"# Report").decode()}]}
    first = manager.operation(owner, "global-resources", {"operation": "global_install", "packages": [package]})
    again = manager.operation(owner, "global-resources", {"operation": "global_install", "packages": [package]})
    assert first == again
    target = Path(first["skills"][0]["path"]) / "SKILL.md"
    assert target.read_text() == "# Report" and target.stat().st_mode & 0o777 == 0o444
    assert manager.operation(owner, "files", {"operation": "list", "path": "."})["entries"]


@pytest.mark.skipif(os.name != "posix", reason="The management interpreter is part of the Linux sandbox image")
def test_management_interpreter_and_standard_library_have_trusted_ancestors(manager):
    paths = {Path(manager.MANAGER_PYTHON[0]), Path(manager.MANAGER_PYTHON[0]).resolve(), Path(sysconfig.get_path("stdlib"))}
    for target in tuple(paths):
        paths.update(target.parents)
    for target in paths:
        info = target.stat()
        assert info.st_uid == 0, str(target)
        assert info.st_mode & 0o022 == 0, str(target)
