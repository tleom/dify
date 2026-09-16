"""Exercise correction and the failure boundary through the real SDK and runner."""

import asyncio

import httpx
import pytest
from pydantic_ai import ModelRetry, Tool
from pydantic_ai.messages import ToolReturnPart

from dify_agent.layers.ask_human.configs import DifyAskHumanLayerConfig
from dify_agent.protocol import RunFailedEvent, RunLayerSpec, RunSucceededEvent
from dify_agent.runtime.runner import AgentRunRunner
from dify_agent.runtime.workbench_tool_recovery import WorkbenchToolFailureLimit, WorkbenchToolRecoveryCapability

from .test_workbench_activity import _call, _progress, _setup


def run_attempt(request, sink):
    async def run():
        async with httpx.AsyncClient() as client:
            try:
                await AgentRunRunner(
                    run_id="native-1",
                    request=request,
                    sink=sink,
                    plugin_daemon_http_client=client,
                    dify_api_http_client=client,
                ).run()
            except WorkbenchToolFailureLimit:
                assert isinstance(sink.events["native-1"][-1], RunFailedEvent)
        return sink.events["native-1"]

    return asyncio.run(run())


@pytest.mark.parametrize("failures", [4, 5])
@pytest.mark.parametrize("kind", ["schema", "execution", "model_retry", "exit_code"])
def test_failure_five_ends_attempt_and_first_four_can_recover(monkeypatch, failures, kind):
    requests = 0
    executed = []

    async def work(value: int):
        executed.append(value)
        if value <= failures:
            if kind == "execution":
                raise RuntimeError("temporary tool failure")
            if kind == "model_retry":
                raise ModelRetry("please fix value")
            if kind == "exit_code":
                return {"exit_code": 1, "done": True}
        return {"exit_code": 0, "done": True}

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests <= failures:
            yield {0: _call("work", {"value": "invalid" if kind == "schema" else requests}, "same-id")}
        elif requests == failures + 1:
            yield {0: _call("work", {"value": 10}, "same-id")}
        else:
            yield "完成"

    request, sink, _ = _setup(monkeypatch, stream, [Tool(work)])
    events = run_attempt(request, sink)
    errors = [item for item in _progress(events, "tool") if item.stage == "error"]
    assert len(errors) == failures
    if failures == 5:
        assert isinstance(events[-1], RunFailedEvent)
        assert "连续调用失败 5 次" in events[-1].data.error
        assert requests == 5
        assert 10 not in executed
    else:
        assert isinstance(events[-1], RunSucceededEvent), events[-1]
        assert requests == 6
        assert executed[-1] == 10
    if kind == "schema":
        assert executed == ([] if failures == 5 else [10])


def test_successful_different_tool_resets_failures_but_report_does_not(monkeypatch):
    sequence = ["bad"] * 4 + ["good"] + ["bad"] * 4 + ["report"] + ["bad"]
    requests = 0

    async def shell_run(script: str):
        raise AssertionError("invalid arguments must never execute")

    async def file_create(path: str, content: str):
        return {"operation": "create", "path": path, "bytes": len(content)}

    async def stream(messages, info):
        nonlocal requests
        kind = sequence[requests]
        requests += 1
        if kind == "good":
            yield {0: _call("file_create", {"path": "fix.py", "content": "pass"}, "file_create")}
        elif kind == "report":
            yield {0: _call("report_activity", {"action": "begin", "title": "正在调整脚本"}, "report")}
        else:
            yield {0: _call("shell_run", {"arguments": '{"script":"python fix.py'}, "shell_run")}

    request, sink, _ = _setup(monkeypatch, stream, [Tool(shell_run), Tool(file_create)])
    events = run_attempt(request, sink)
    assert isinstance(events[-1], RunFailedEvent)
    assert requests == len(sequence)
    assert len([item for item in _progress(events, "tool") if item.stage == "error"]) == 9


def test_fifth_failure_allows_an_already_running_parallel_tool_to_settle(monkeypatch):
    started, exhausted = asyncio.Event(), asyncio.Event()
    requests = 0
    completed = []
    original_observe = WorkbenchToolRecoveryCapability.observe

    def observe(capability, event):
        original_observe(capability, event)
        if capability.exhausted:
            exhausted.set()

    monkeypatch.setattr(WorkbenchToolRecoveryCapability, "observe", observe)

    async def bad(index: int):
        if index == 5:
            await asyncio.wait_for(started.wait(), timeout=2)
        raise RuntimeError("known failure")

    async def slow():
        started.set()
        await asyncio.wait_for(exhausted.wait(), timeout=2)
        completed.append("slow")
        return "already running work completed"

    async def later():
        completed.append("later")
        return "must not start"

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests < 5:
            yield {0: _call("bad", {"index": requests}, f"bad-{requests}")}
        else:
            yield {
                0: _call("slow", {}, "slow"),
                1: _call("bad", {"index": 5}, "bad-5"),
                2: _call("later", {}, "later"),
            }

    request, sink, _ = _setup(monkeypatch, stream, [Tool(bad), Tool(slow), Tool(later, sequential=True)])
    events = run_attempt(request, sink)
    assert completed == ["slow"]
    assert requests == 5
    assert isinstance(events[-1], RunFailedEvent)
    from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState

    snapshot = events[-1].data.session_snapshot
    history = PydanticAIHistoryRuntimeState.model_validate(
        next(layer for layer in snapshot.layers if layer.name == "history").runtime_state
    )
    results = [part for message in history.messages for part in message.parts if isinstance(part, ToolReturnPart)]
    assert len(results) == 7
    assert next(part for part in results if part.tool_name == "slow").content == "already running work completed"
    skipped = next(part for part in results if part.tool_name == "later")
    assert skipped.metadata["workbench_budget_skipped"] is True


def test_fifth_failure_skips_unstarted_sequential_calls_and_retains_every_outcome(monkeypatch):
    executed = []
    requests = 0

    async def work(index: int):
        executed.append(index)
        raise RuntimeError("known failure")

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        yield {index: _call("work", {"index": index}, f"work-{index}") for index in range(8)}

    request, sink, _ = _setup(monkeypatch, stream, [Tool(work, sequential=True)])
    events = run_attempt(request, sink)
    assert executed == list(range(5))
    assert requests == 1
    assert isinstance(events[-1], RunFailedEvent)
    from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState

    snapshot = events[-1].data.session_snapshot
    history = PydanticAIHistoryRuntimeState.model_validate(
        next(layer for layer in snapshot.layers if layer.name == "history").runtime_state
    )
    results = [part for message in history.messages for part in message.parts if isinstance(part, ToolReturnPart)]
    assert len(results) == 8
    skipped = [
        part for part in results if isinstance(part.metadata, dict) and part.metadata.get("workbench_budget_skipped")
    ]
    assert len(skipped) == 3
    assert all("本次操作未执行" in str(part.content) for part in skipped)


@pytest.mark.parametrize("failures", [4, 5])
def test_ask_human_default_mismatch_is_returned_for_correction(monkeypatch, failures):
    requests = 0
    observations = []

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        observations.extend(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.outcome == "failed"
        )
        yield {
            0: _call(
                "ask_human",
                {
                    "question": "请选择测算方式",
                    "fields": [
                        {
                            "name": "mode",
                            "type": "select",
                            "label": "方式",
                            "options": [{"value": "equal", "label": "等额"}],
                            "default": "incorrect" if requests <= failures else "equal",
                        }
                    ],
                },
                "question",
            )
        }

    request, sink, _ = _setup(monkeypatch, stream)
    request.composition.layers.append(
        RunLayerSpec(
            name="ask_human",
            type="dify.ask_human",
            config=DifyAskHumanLayerConfig(),
        )
    )
    events = run_attempt(request, sink)
    assert any("default must match" in str(item) and "fields.0.select" in str(item) for item in observations)
    assert requests == 5
    if failures == 4:
        assert isinstance(events[-1], RunSucceededEvent), events[-1]
        assert events[-1].data.deferred_tool_call.args["fields"][0]["default"] == "equal"
    else:
        assert isinstance(events[-1], RunFailedEvent), events[-1]
        assert "连续调用失败 5 次" in events[-1].data.error


def test_unknown_tool_counter_resets_after_success_of_another_tool(monkeypatch):
    requests = 0

    async def good():
        return "ok"

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests in (5, 10):
            yield {0: _call("good", {}, str(requests))}
        elif requests == 11:
            yield "完成"
        else:
            yield {0: _call("missing_tool", {}, str(requests))}

    request, sink, _ = _setup(monkeypatch, stream, [Tool(good)])
    events = run_attempt(request, sink)
    assert isinstance(events[-1], RunSucceededEvent), events[-1]
    assert requests == 11


def test_unknown_tool_fails_at_five_with_history_saved(monkeypatch):
    requests = 0

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        yield {0: _call("missing_tool", {}, str(requests))}

    request, sink, _ = _setup(monkeypatch, stream)
    events = run_attempt(request, sink)
    assert isinstance(events[-1], RunFailedEvent), events[-1]
    assert requests == 5
    assert "连续调用失败 5 次" in events[-1].data.error


def test_multiple_deferred_questions_return_correction_instead_of_crashing(monkeypatch):
    requests = 0

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        yield {
            index: _call("ask_human", {"question": f"问题 {index}"}, f"call-{requests}-{index}")
            for index in range(2 if requests == 1 else 1)
        }

    request, sink, _ = _setup(monkeypatch, stream)
    request.composition.layers.append(
        RunLayerSpec(
            name="ask_human",
            type="dify.ask_human",
            config=DifyAskHumanLayerConfig(),
        )
    )
    events = run_attempt(request, sink)
    assert isinstance(events[-1], RunSucceededEvent), events[-1]
    assert requests == 2
    assert events[-1].data.deferred_tool_call.args["question"] == "问题 0"
