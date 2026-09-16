import asyncio
import base64
import hashlib
import json
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from dify_agent.runtime.workbench_tool_output import _PROGRAM, WorkbenchOverflowStore, WorkbenchToolOutputLimits
from dify_agent.runtime.workbench_tool_recovery import WorkbenchToolFailureLimit, WorkbenchToolRecoveryCapability


class LocalShell:
    """Execute the actual sandbox helper with the test interpreter, without HTTP."""

    def __init__(self, root):
        self.root = root

    async def run_remote_script_complete(self, script, *, timeout, max_output_bytes):
        parts = shlex.split(script)
        assert parts[0] == "python3"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *parts[1:],
            cwd=self.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        assert not stderr
        return SimpleNamespace(
            exit_code=process.returncode,
            output=stdout.decode(),
            output_complete=len(stdout) <= max_output_bytes,
        )


def test_large_output_survives_chunks_and_a_new_store_but_not_another_conversation(tmp_path):
    source = tmp_path / "one"
    other = tmp_path / "two"
    source.mkdir()
    other.mkdir()
    data = ('中文"\\\n' * 20_000).encode()

    async def exercise():
        handle = await WorkbenchOverflowStore(LocalShell(source)).write("native/tool", data)
        reopened = WorkbenchOverflowStore(LocalShell(source))
        assert await reopened.read(handle) == data
        with pytest.raises(OSError):
            await WorkbenchOverflowStore(LocalShell(other)).read(handle)
        with pytest.raises(OSError):
            await reopened.read("../one")
        assert not list(source.rglob("*.partial"))

    asyncio.run(exercise())


def test_real_sdk_spills_output_and_reads_the_middle_of_one_long_line(tmp_path):
    payload = "a" * 20_000 + "critical-middle-value" + "z" * 20_000
    calls = 0
    read_output = None

    def huge():
        return payload

    def model(messages, info):
        nonlocal calls, read_output
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("huge", {}, "huge-call")])
        part = next(part for part in messages[-1].parts if isinstance(part, ToolReturnPart))
        if calls == 2:
            assert len(part.content) < 2_000
            handle = part.metadata["overflow_handle"]
            assert "stored to handle" in part.content
            assert any(tool.name == "read_tool_result" for tool in info.function_tools)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_tool_result",
                        {"handle": handle, "char_offset": 20_000},
                        "read-call",
                    )
                ]
            )
        read_output = json.loads(part.content)
        return ModelResponse(parts=[TextPart("checked")])

    capability = WorkbenchToolOutputLimits(LocalShell(tmp_path), input_budget=2_000)
    result = Agent(FunctionModel(model), tools=[Tool(huge)]).run_sync("continue", capabilities=[capability])
    assert result.output == "checked"
    assert read_output["text"].startswith("critical-middle-value")
    assert len(read_output["text"]) <= 2_000
    assert read_output["next_char_offset"] == 22_000
    stored = next((tmp_path / ".cache/workbench-tool-results").iterdir())
    assert stored.read_text() == payload


def test_spilled_business_errors_still_exhaust_the_five_failure_budget(tmp_path):
    attempts = 0

    def fail():
        nonlocal attempts
        attempts += 1
        return {"error": "provider error " * 2_000}

    def model(messages, info):
        return ModelResponse(parts=[ToolCallPart("fail", {}, f"call-{attempts}")])

    async def stream(messages, info):
        yield {0: DeltaToolCall(name="fail", json_args="{}", tool_call_id=f"call-{attempts}")}

    recovery = WorkbenchToolRecoveryCapability()

    async def observe(ctx, events):
        async for event in events:
            recovery.observe(event)

    capability = WorkbenchToolOutputLimits(LocalShell(tmp_path), input_budget=2_000)
    with pytest.raises(WorkbenchToolFailureLimit):
        Agent(FunctionModel(model, stream_function=stream), tools=[Tool(fail)]).run_sync(
            "continue",
            capabilities=[capability, recovery],
            event_stream_handler=observe,
        )
    assert attempts == 5
    assert recovery.consecutive_failures == 5


def test_storage_failure_returns_explicit_truncation_without_a_false_handle(tmp_path):
    class FailedShell(LocalShell):
        async def run_remote_script_complete(self, *args, **kwargs):
            raise OSError("sandbox temporarily unavailable")

    calls = 0

    def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("large", {}, "large-call")])
        part = next(part for part in messages[-1].parts if isinstance(part, ToolReturnPart))
        assert len(part.content) <= 2_000
        assert "stored to handle" not in part.content
        assert "omitted" in part.content
        return ModelResponse(parts=[TextPart("received bounded result")])

    def large():
        return "full-output" * 5_000

    capability = WorkbenchToolOutputLimits(FailedShell(tmp_path), input_budget=2_000)
    result = Agent(FunctionModel(model), tools=[Tool(large)]).run_sync("continue", capabilities=[capability])
    assert result.output == "received bounded result"


@pytest.mark.parametrize(
    ("pattern", "from_end", "offset", "limit", "char_offset"),
    [
        (None, False, 0, 20, 0),
        (None, False, 2, 4, 3),
        (None, True, 1, 3, 0),
        ("needle", False, 0, 3, 2),
        ("中文", True, 0, 2, 1),
        ("", False, 0, 2, 100),
        ("absent", False, 0, 1, 0),
        (None, False, 100, 5, 0),
    ],
)
def test_streamed_slices_preserve_line_filter_and_character_offset_semantics(
    tmp_path, pattern, from_end, offset, limit, char_offset
):
    data = "\n零\r\n一needle\v二中文\f三needle\x1c四\x1d五\x1e六\x85七中文\u2028八\u2029尾\n".encode() + b"bad\xff"
    lines = data.decode("utf-8", errors="replace").splitlines()
    if pattern is not None:
        lines = [line for line in lines if pattern in line]
    end = max(0, len(lines) - offset) if from_end else min(len(lines), offset + limit)
    start = max(0, end - limit) if from_end else offset
    expected = "\n".join(lines[start:end])
    last = min(len(expected), char_offset + 7)

    async def exercise():
        store = WorkbenchOverflowStore(LocalShell(tmp_path))
        handle = await store.write("lines", data)
        result = json.loads(
            await store.read_slice(
                handle,
                pattern=pattern,
                from_end=from_end,
                offset=offset,
                limit=limit,
                char_offset=char_offset,
                max_chars=7,
            )
        )
        assert result == {
            "handle": handle,
            "matching_lines": len(lines),
            "line_offset": start,
            "next_char_offset": last if last < len(expected) else None,
            "text": expected[char_offset:last],
        }

    asyncio.run(exercise())


def test_commit_and_long_line_reads_use_bounded_memory_in_the_actual_helper(tmp_path):
    handle = "a" * 64
    directory = tmp_path / ".cache" / "workbench-tool-results"
    directory.mkdir(parents=True)
    partial = directory / (handle + ".partial")
    digest = hashlib.sha256()
    # Put the literal search string across a 64 KiB text chunk boundary.
    prefix = b"header\n" + b"a" * (64 * 1024 - 9)
    marker = "needle-中文".encode()
    with partial.open("wb") as stream:
        for chunk in (prefix, marker, *([b"z" * (64 * 1024)] * 128), b"\ntail needle\n"):
            stream.write(chunk)
            digest.update(chunk)
    size = partial.stat().st_size
    instrumented = (
        "import tracemalloc\nfrom pathlib import Path\ntracemalloc.start()\ntry:\n"
        f"    exec({_PROGRAM!r})\n"
        "finally:\n    Path('memory-peak.txt').write_text(str(tracemalloc.get_traced_memory()[1]))\n"
    )

    def operation(name, **values):
        payload = base64.b64encode(json.dumps({"operation": name, "handle": handle, **values}).encode()).decode()
        process = subprocess.run(
            [sys.executable, "-c", instrumented, payload], cwd=tmp_path, capture_output=True, check=True
        )
        # The helper must not allocate an entire 8 MiB file or single line.
        assert int((tmp_path / "memory-peak.txt").read_text()) < 4 * 1024 * 1024
        return json.loads(process.stdout)

    operation("commit", size=size, digest=digest.hexdigest())
    assert not partial.exists()
    assert (directory / handle).stat().st_size == size
    args = {
        "pattern": "needle",
        "offset": 0,
        "limit": 1,
        "from_end": False,
        "char_offset": 64 * 1024 - 9,
        "max_chars": 64,
    }
    middle = json.loads(operation("slice", **args)["text"])
    assert middle["matching_lines"] == 2
    assert middle["text"].startswith("needle-中文")
    assert len(middle["text"]) == 64
    assert middle["next_char_offset"] == args["char_offset"] + 64
    tail = json.loads(operation("slice", **{**args, "from_end": True, "char_offset": 0})["text"])
    assert tail == {
        "handle": handle,
        "matching_lines": 2,
        "line_offset": 1,
        "next_char_offset": None,
        "text": "tail needle",
    }
