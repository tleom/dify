import json
import shlex
import subprocess
import sys

import pytest

from dify_agent.layers.shell.file_operations import file_script


def execute(root, operation, **args):
    command = shlex.split(file_script(str(root), operation, **args))
    result = subprocess.run([sys.executable, *command[1:]], capture_output=True, text=True, encoding="utf-8")
    return result.returncode, json.loads(result.stdout)


def test_create_and_exact_edit_preserve_utf8_and_reject_ambiguous_changes(tmp_path):
    content = '中文\r\nprint("hello")\r\n'
    code, result = execute(tmp_path, "create", path="script.py", content=content)
    assert code == 0 and result["operation"] == "create"
    path = tmp_path / "script.py"
    assert path.read_bytes() == content.encode()
    assert execute(tmp_path, "create", path="script.py", content="overwrite")[0] == 1
    assert path.read_bytes() == content.encode()
    assert execute(tmp_path, "edit", path="script.py", old_text="\r\n", new_text="broken")[0] == 1
    assert path.read_bytes() == content.encode()
    code, result = execute(tmp_path, "edit", path="script.py", old_text='print("hello")', new_text='print("世界")')
    assert code == 0 and result["operation"] == "edit"
    assert path.read_bytes() == '中文\r\nprint("世界")\r\n'.encode()


def test_file_operation_rejects_escape_and_oversized_command(tmp_path):
    code, result = execute(tmp_path, "create", path="../outside.txt", content="bad")
    assert code == 1 and "workspace" in result["error"]
    assert not (tmp_path.parent / "outside.txt").exists()
    with pytest.raises(ValueError, match="64 KiB"):
        file_script(str(tmp_path), "create", path="large.txt", content="x" * (65 * 1024))
