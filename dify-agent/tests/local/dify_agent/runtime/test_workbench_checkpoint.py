import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic_ai import Tool
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart

from dify_agent.runtime.workbench_checkpoint import WorkbenchHistoryCheckpoint

from .test_workbench_activity import _call, _setup
from .test_workbench_tool_recovery import run_attempt


def test_checkpoints_strip_transient_instructions_and_preserve_pending_calls():
    sink = SimpleNamespace(checkpoint_history=AsyncMock())
    checkpoint = WorkbenchHistoryCheckpoint(sink=sink, run_id="native")
    messages = [
        ModelRequest(parts=[UserPromptPart("完成报告")], instructions="temporary credentials"),
        ModelResponse(parts=[ToolCallPart("send", {"value": 1}, "call")]),
    ]

    async def check():
        await checkpoint.save(messages)
        await checkpoint.save(messages)

    asyncio.run(check())
    sink.checkpoint_history.assert_awaited_once()
    run_id, data = sink.checkpoint_history.call_args.args
    assert run_id == "native"
    assert "temporary credentials" not in data
    assert '"tool_call_id":"call"' in data


def test_real_runner_checkpoints_before_tool_execution_and_after_results(monkeypatch):
    requests = 0
    executed = []

    async def write(value: int):
        checkpoint = json.loads(sink.history_checkpoints["native-1"])
        assert checkpoint["messages"][-1]["parts"][0]["tool_name"] == "write"
        executed.append(value)
        return {"saved": value}

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: _call("write", {"value": 1}, "write-call")}
        else:
            assert any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)
            assert '"saved":1' in sink.history_checkpoints["native-1"]
            yield "完成"

    request, sink, _ = _setup(monkeypatch, stream, [Tool(write)])
    events = run_attempt(request, sink)
    assert events[-1].type == "run_succeeded"
    assert executed == [1]


def test_compacted_checkpoint_keeps_delivery_cursor_even_when_history_is_unchanged():
    sink = SimpleNamespace(checkpoint_history=AsyncMock())
    delivered = {"first"}
    checkpoint = WorkbenchHistoryCheckpoint(sink=sink, run_id="native", seen_ids=delivered)
    summary = [ModelRequest(parts=[UserPromptPart("压缩后的进度摘要")])]

    async def check():
        await checkpoint.save(summary)
        delivered.add("second")
        await checkpoint.save(summary)

    asyncio.run(check())
    assert sink.checkpoint_history.await_count == 2
    state = json.loads(sink.checkpoint_history.call_args.args[1])
    assert state["steering_delivered_ids"] == ["first", "second"]
    assert not state["messages"][0].get("metadata")
