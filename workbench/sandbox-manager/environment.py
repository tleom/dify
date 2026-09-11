"""Runs in a disposable installer with only this user's environment volume writable."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path("/opt/user-env")
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:(?:==|~=|>=|<=|>|<)[A-Za-z0-9_.+*-]+)?$")
NODE_PACKAGE = re.compile(r"^(?:@[a-z0-9_.-]+/)?[a-z0-9][a-z0-9_.-]*(?:@[a-zA-Z0-9_.^~*-]+)?$")


def run(args):
    subprocess.run(args, check=True, timeout=600, stdout=sys.stderr, stderr=sys.stderr)


def install(payload):
    python_packages = payload.get("python", [])
    node_packages = payload.get("node", [])
    if len(python_packages) + len(node_packages) > 50:
        raise ValueError("At most 50 packages per update")
    if any(not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in python_packages):
        raise ValueError("Use registry Python package names with optional versions")
    if any(not isinstance(p, str) or not NODE_PACKAGE.fullmatch(p) for p in node_packages):
        raise ValueError("Use registry Node package names with optional versions")
    current = ROOT / "current"
    versions = ROOT / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    stage = versions / uuid.uuid4().hex
    stage.mkdir()
    try:
        run([sys.executable, "-m", "venv", str(stage / "python")])
        py = str(stage / "python/bin/python")
        if current.exists():
            frozen = subprocess.check_output([str(current / "python/bin/python"), "-m", "pip", "freeze"], text=True)
            if frozen.strip():
                requirements = stage / "requirements.txt"
                requirements.write_text(frozen)
                run([py, "-m", "pip", "install", "-r", str(requirements)])
            if (current / "node").exists():
                shutil.copytree(current / "node", stage / "node", symlinks=True)
        (stage / "node").mkdir(exist_ok=True)
        if python_packages:
            run([py, "-m", "pip", "install", "--upgrade", *python_packages])
        run([py, "-m", "pip", "check"])
        if node_packages:
            run(["npm", "install", "--ignore-scripts", "--prefix", str(stage / "node"), "--", *node_packages])
        if (stage / "node/package.json").exists():
            run(["npm", "ls", "--prefix", str(stage / "node"), "--all"])
        (stage / "request.json").write_text(json.dumps(payload))
        link = ROOT / (".next-" + uuid.uuid4().hex)
        link.symlink_to(stage)
        os.replace(link, current)
        return {"version": stage.name, "status": "ready"}
    except BaseException:
        shutil.rmtree(stage)
        raise


if __name__ == "__main__":
    print(json.dumps(install(json.load(sys.stdin))))
