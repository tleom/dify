"""Bounded command capture inside the disposable read-only planning container."""

import base64
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid

MAX_CAPTURE = 10 * 1024 * 1024
PREVIEW_BYTES = 16 * 1024


def preview(path):
    target = Path(path).resolve(strict=True)
    if not target.is_relative_to("/tmp") or not target.is_file():
        raise ValueError("预览文件必须位于 /tmp 调查目录")
    if target.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("预览图片超过 2 MiB，请缩小后再读取")
    with target.open("rb") as source:
        data = source.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("预览图片超过 2 MiB，请缩小后再读取")
    media_type = (
        "image/png"
        if data.startswith(b"\x89PNG\r\n\x1a\n")
        else ("image/jpeg" if data.startswith(b"\xff\xd8\xff") else None)
    )
    if media_type is None:
        raise ValueError("计划预览仅支持 PNG 或 JPEG 图片")
    return {
        "path": str(target),
        "media_type": media_type,
        "data": base64.b64encode(data).decode(),
    }


def investigate(payload):
    # Use unique names rather than following a previous command's symlink.
    for directory in ("/tmp/home", "/tmp/cache"):
        Path(directory).mkdir(exist_ok=True)
    log = Path("/tmp") / ("inspection-" + uuid.uuid4().hex + ".log")
    started = time.monotonic()
    timed_out = False
    truncated = False
    size = 0
    process = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-c", payload["script"]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def stop_group():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        with log.open("xb") as output, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() - started >= payload["timeout"]:
                    timed_out = True
                    stop_group()
                    break
                ready = selector.select(timeout=0.1)
                for key, _ in ready:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    keep = min(len(chunk), max(0, MAX_CAPTURE - size))
                    output.write(chunk[:keep])
                    size += keep
                    truncated = truncated or keep != len(chunk)
        remaining = max(0.1, payload["timeout"] - (time.monotonic() - started))
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_group()
            process.wait()
    finally:
        stop_group()
        if process.poll() is None:
            process.wait()
        process.stdout.close()
    with log.open("rb") as source:
        output = source.read(PREVIEW_BYTES)
        if size > PREVIEW_BYTES:
            source.seek(max(PREVIEW_BYTES, size - PREVIEW_BYTES))
            output += (
                b"\n... [read output_path for remaining captured output] ...\n"
                + source.read(PREVIEW_BYTES)
            )
    images, warnings = [], []
    for path in payload.get("preview_paths", []):
        try:
            images.append(preview(path))
        except (OSError, ValueError) as error:
            warnings.append(str(error))
    return {
        "output": output.decode("utf-8", errors="replace"),
        "output_path": str(log),
        "output_truncated": truncated or size > PREVIEW_BYTES,
        "exit_code": 124 if timed_out else process.returncode,
        "timed_out": timed_out,
        "previews": images,
        "warnings": warnings,
    }


if __name__ == "__main__":
    print(json.dumps(investigate(json.load(sys.stdin)), ensure_ascii=False))
