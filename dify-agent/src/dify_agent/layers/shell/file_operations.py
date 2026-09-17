"""Small, explicit text-file operations executed inside the active shell sandbox."""

import base64
import json
import shlex


def file_script(root: str, operation: str, **arguments: str) -> str:
    encoded = json.dumps({"root": root, "operation": operation, **arguments}, ensure_ascii=False).encode()
    if len(encoded) > 64 * 1024:
        raise ValueError(
            "Each file operation accepts at most 64 KiB of arguments; split the content into smaller edits"
        )
    payload = base64.b64encode(encoded).decode()
    return f"python3 -c {shlex.quote(_PROGRAM)} {shlex.quote(payload)}"


_PROGRAM = """
import base64, json, os, stat, sys, tempfile
from pathlib import Path

try:
    args = json.loads(base64.b64decode(sys.argv[1]))
    presentation = {}
    root = Path(args["root"]).resolve(strict=True)
    boundary = Path('/workspace') if root.is_relative_to('/workspace') else root
    path = Path(args["path"])
    path = (path if path.is_absolute() else root / path).resolve()
    if path == boundary or not path.is_relative_to(boundary):
        raise ValueError("File path must stay inside the workspace")
    if args["operation"] == "create":
        content = args["content"]
        if len(content.encode("utf-8")) > 1024 * 1024:
            raise ValueError("File content exceeds 1 MiB; split the operation")
        with path.open("x", encoding="utf-8", newline="") as stream:
            stream.write(content)
    else:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("File exceeds the text edit limit of 1 MiB")
        original = path.read_bytes()
        content = original.decode("utf-8")
        old = args["old_text"]
        if not old or content.count(old) != 1:
            raise ValueError("old_text must match exactly once; read the file and provide unique surrounding text")
        if old == args["new_text"]:
            raise ValueError("old_text and new_text must differ")
        start = content.index(old)
        prefix, suffix = content[:start], content[start + len(old):]
        # Actual applied replacement with three surrounding lines, matching
        # DSH's result-time diff. Bound unusually long context lines.
        left = ''.join(prefix.splitlines(keepends=True)[-3 if prefix.endswith(chr(10)) else -4:])
        right = ''.join(suffix.splitlines(keepends=True)[:4])
        if len((left + right).encode('utf-8')) > 32 * 1024:
            left, right = '', ''
        presentation = {"diffs": [{"oldText": left + old + right, "newText": left + args["new_text"] + right}]}
        content = content.replace(old, args["new_text"], 1)
        if len(content.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Edited content exceeds 1 MiB")
        mode = stat.S_IMODE(path.stat().st_mode)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".workbench-edit-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.chmod(temporary, mode)
            if path.read_bytes() != original:
                raise ValueError("File changed during editing; read it again")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(json.dumps({"path": str(path), "operation": args["operation"], "bytes": path.stat().st_size, **presentation}))
except Exception as error:
    print(json.dumps({"error": str(error)}))
    sys.exit(1)
"""
