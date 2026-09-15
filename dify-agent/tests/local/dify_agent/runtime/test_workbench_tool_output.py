import asyncio
import json
import shlex
import sys
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from dify_agent.runtime.workbench_tool_output import WorkbenchOverflowStore, WorkbenchToolOutputLimits
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
