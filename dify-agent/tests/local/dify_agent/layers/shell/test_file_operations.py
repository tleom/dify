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
    assert result["diffs"] == [{"oldText": content, "newText": '中文\r\nprint("世界")\r\n'}]


def test_edit_reports_applied_context_and_rejects_no_op_without_changing_file(tmp_path):
    path = tmp_path / "report.txt"
    lines = [f"line {number}\n" for number in range(20)]
    path.write_bytes("".join(lines).encode("utf-8"))
    code, result = execute(tmp_path, "edit", path="report.txt", old_text="line 10", new_text="changed 10")
    assert code == 0
    assert result["diffs"][0]["oldText"] == "".join(lines[7:14])
    assert result["diffs"][0]["newText"] == "".join(lines[7:14]).replace("line 10", "changed 10")
    original = path.read_bytes()
    assert execute(tmp_path, "edit", path="report.txt", old_text="changed 10", new_text="changed 10")[0] == 1
    assert path.read_bytes() == original


def test_file_operation_rejects_escape_and_oversized_command(tmp_path):
    code, result = execute(tmp_path, "create", path="../outside.txt", content="bad")
    assert code == 1 and "workspace" in result["error"]
    assert not (tmp_path.parent / "outside.txt").exists()
    with pytest.raises(ValueError, match="64 KiB"):
        file_script(str(tmp_path), "create", path="large.txt", content="x" * (65 * 1024))
