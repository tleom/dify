"""Exercise silent-model recovery through the real SDK lifecycle and runner."""

import asyncio

import httpx
import pytest
from pydantic_ai import Tool
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ToolReturnPart

from dify_agent.protocol import RunFailedEvent, RunSucceededEvent
from dify_agent.runtime import runner
from dify_agent.runtime.workbench_model_idle import WorkbenchModelIdleCapability

from .test_workbench_activity import _call, _setup


async def _run(request, sink):
    async with httpx.AsyncClient() as client:
        return await runner.AgentRunRunner(
            run_id="idle-test",
            request=request,
            sink=sink,
            plugin_daemon_http_client=client,
            dify_api_http_client=client,
            stream_text_delta_coalescing_enabled=False,
        ).run()


@pytest.fixture(autouse=True)
def short_deadline(monkeypatch):
    monkeypatch.setattr(
        runner, "WorkbenchModelIdleCapability", lambda: WorkbenchModelIdleCapability(timeout_seconds=0.2)
    )


@pytest.mark.parametrize("partial", [False, True])
def test_silent_model_ends_attempt_and_preserves_received_history(monkeypatch, partial):
    async def stream(messages, info):
        if partial:
            yield "已经核对第一项。"
        await asyncio.sleep(10)
        yield "unreachable"

    request, sink, _ = _setup(monkeypatch, stream)
    with pytest.raises(UsageLimitExceeded, match="模型连续 0.2 秒未返回内容"):
        asyncio.run(_run(request, sink))
    event = sink.events["idle-test"][-1]
    assert isinstance(event, RunFailedEvent)
    assert event.data.session_snapshot is not None
    if partial:
        history = next(layer for layer in event.data.session_snapshot.layers if layer.name == "history")
        assert "已经核对第一项" in str(history.runtime_state)


def test_active_stream_resets_deadline_beyond_one_timeout_window(monkeypatch):
    async def stream(messages, info):
        for _ in range(6):
            yield "持续返回内容。"
            await asyncio.sleep(0.06)

    request, sink, _ = _setup(monkeypatch, stream)
    asyncio.run(_run(request, sink))
    assert isinstance(sink.events["idle-test"][-1], RunSucceededEvent)


def test_long_tool_is_excluded_and_next_silent_model_request_is_bounded(monkeypatch):
    completed = []
    calls = 0

    async def slow_work():
        await asyncio.sleep(0.35)
        completed.append("done")
        return "工作已完成"

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: _call("slow_work", {}, "slow-1")}
        else:
            assert any(
                isinstance(part, ToolReturnPart) and part.content == "工作已完成"
                for message in messages
                for part in message.parts
            )
            await asyncio.sleep(10)
            yield "unreachable"

    request, sink, _ = _setup(monkeypatch, stream, [Tool(slow_work)])
    with pytest.raises(UsageLimitExceeded, match="模型连续"):
        asyncio.run(_run(request, sink))
    assert completed == ["done"]
    assert calls == 2


def test_manual_cancellation_remains_cancellation(monkeypatch):
    started = asyncio.Event()

    async def stream(messages, info):
        started.set()
        await asyncio.sleep(10)
        yield "unreachable"

    request, sink, _ = _setup(monkeypatch, stream)

    async def scenario():
        task = asyncio.create_task(_run(request, sink))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not any(
        isinstance(event, RunFailedEvent) and "模型连续" in event.data.error for event in sink.events["idle-test"]
    )


def test_unrelated_timeout_is_not_relabelled():
    async def scenario():
        async with WorkbenchModelIdleCapability().guard():
            raise TimeoutError("external service timeout")

    with pytest.raises(TimeoutError, match="external service timeout"):
        asyncio.run(scenario())
