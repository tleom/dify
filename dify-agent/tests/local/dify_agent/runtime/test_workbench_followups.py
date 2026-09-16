"""Exercise supplements through the real Agent runner, SDK graph and HTTP layer."""

import asyncio
import json

import httpx
import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import UserPromptPart

from dify_agent.protocol import RUN_EVENT_ADAPTER, DeferredToolResultsPayload, RunLayerSpec, RunSucceededEvent
from dify_agent.runtime.runner import AgentRunRunner

from .test_workbench_activity import _call, _setup


@pytest.mark.parametrize("continuation", ["继续", "改为先检查附件"])
def test_stopped_run_continues_from_captured_context_without_repeating_completed_tool(monkeypatch, continuation):
    from pydantic_ai.messages import ToolReturnPart

    executed, received = [], []
    stopped = asyncio.Event()
    continuing = False

    async def work():
        executed.append("saved-file")
        return "file is already saved"

    async def stream(messages, info):
        prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        received.append(prompts)
        if continuing:
            assert "hello" in prompts
            assert continuation in prompts
            assert any(
                isinstance(part, ToolReturnPart) and part.content == "file is already saved"
                for message in messages
                for part in message.parts
            )
            yield "沿原上下文继续完成。"
        elif len(received) == 1:
            yield {0: _call("work", {}, "save-once")}
        else:
            stopped.set()
            await asyncio.Event().wait()

    request, sink, _ = _setup(monkeypatch, stream, [Tool(work)])
    context = next(layer for layer in request.composition.layers if layer.name == "execution_context")
    context.config = {**dict(context.config), "workbench_run_id": "turn-1", "user_id": "account-1", "app_id": "app-1"}
    request.composition.layers.append(
        RunLayerSpec(
            name="workbench_followups",
            type="dify.workbench_followups",
            deps={"execution_context": "execution_context"},
        )
    )

    def transport(request):
        data = json.loads(request.content)
        return httpx.Response(200, json={"messages": [], "sealed": data["action"] == "seal"})

    async def execute():
        nonlocal continuing
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            original = AgentRunRunner(
                run_id="paused-native",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            )
            task = asyncio.create_task(original.run())
            await asyncio.wait_for(stopped.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert original.terminal_session_snapshot is not None
            request.session_snapshot = original.terminal_session_snapshot
            context.config = {**dict(context.config), "workbench_run_id": "turn-2"}
            next(layer for layer in request.composition.layers if layer.name == "prompt").config = {
                "prefix": "system",
                "user": continuation,
            }
            continuing = True
            await AgentRunRunner(
                run_id="continued-native",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(execute())
    assert isinstance(sink.events["continued-native"][-1], RunSucceededEvent)
    assert executed == ["saved-file"]


@pytest.mark.parametrize("arrival", ["before_model", "during_tool", "during_final"])
def test_supplement_reaches_same_run_once_without_replaying_work(monkeypatch, arrival):
    pending = []
    received = []
    requests = []
    executed = []
    supplement = {"id": "queued-message", "content": "这是对原任务的补充：把报告改为横版。"}

    async def work():
        executed.append("work")
        if arrival == "during_tool":
            pending.append(supplement)
        return "finished original operation"

    async def stream(messages, info):
        prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        received.append(prompts)
        if len(received) == 1:
            yield {0: _call("work", {}, "original-work")}
        else:
            if arrival == "during_final" and len(received) == 2:
                pending.append(supplement)
            yield "报告已调整。" if supplement["content"] in prompts else "原操作已完成。"

    def transport(request):
        assert request.url.path == "/inner/api/agent/workbench/followups"
        data = json.loads(request.content)
        requests.append(data)
        assert data["backend_run_id"] == "native-followup"
        assert data["workbench_run_id"] == "turn-1"
        if arrival == "before_model" and not pending:
            pending.append(supplement)
        messages = [item for item in pending if item["id"] not in data["seen_ids"]]
        return httpx.Response(200, json={"messages": messages, "sealed": data["action"] == "seal" and not messages})

    request, sink, _ = _setup(monkeypatch, stream, [Tool(work)])
    context = next(layer for layer in request.composition.layers if layer.name == "execution_context")
    context.config = {**dict(context.config), "workbench_run_id": "turn-1", "user_id": "account-1", "app_id": "app-1"}
    request.composition.layers.append(
        RunLayerSpec(
            name="workbench_followups", type="dify.workbench_followups", deps={"execution_context": "execution_context"}
        )
    )

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="native-followup",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(execute())
    events = sink.events["native-followup"]
    assert isinstance(events[-1], RunSucceededEvent), events[-1]
    for event in events:
        assert RUN_EVENT_ADAPTER.validate_json(RUN_EVENT_ADAPTER.dump_json(event)) == event
    assert executed == ["work"]
    assert supplement["content"] in received[-1]
    assert all(prompts.count(supplement["content"]) <= 1 for prompts in received)
    assert requests[-1]["action"] == "seal"
    assert requests[-1]["seen_ids"] == ["queued-message"]
    state = next(
        layer.runtime_state for layer in events[-1].data.session_snapshot.layers if layer.name == "workbench_followups"
    )
    assert state["seen_ids"] == ["queued-message"]
    assert len(received) == (3 if arrival == "during_final" else 2)


def test_paused_task_resumes_with_unseen_supplement_and_keeps_delivery_cursor(monkeypatch):
    pending = [{"id": "first", "content": "补充一：保留原文件。"}]
    model_calls = 0
    http_calls = []

    async def stream(messages, info):
        nonlocal model_calls
        model_calls += 1
        prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        assert prompts.count(pending[0]["content"]) == 1
        if model_calls == 1:
            yield {0: _call("update_shared_environment", {"reason": "读取文档", "python": ["python-docx"]}, "install")}
        else:
            assert prompts.count(pending[1]["content"]) == 1
            yield "按照两条补充继续处理。"

    def transport(request):
        data = json.loads(request.content)
        http_calls.append(data)
        messages = [item for item in pending if item["id"] not in data["seen_ids"]]
        return httpx.Response(200, json={"messages": messages, "sealed": data["action"] == "seal" and not messages})

    request, sink, _ = _setup(monkeypatch, stream)
    context = next(layer for layer in request.composition.layers if layer.name == "execution_context")
    context.config = {**dict(context.config), "workbench_run_id": "turn-1", "user_id": "account-1", "app_id": "app-1"}
    request.composition.layers.extend(
        [
            RunLayerSpec(
                name="workbench_followups",
                type="dify.workbench_followups",
                deps={"execution_context": "execution_context"},
            ),
            RunLayerSpec(name="workbench_environment", type="dify.workbench_environment", config={}),
        ]
    )

    async def execute(identifier):
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id=identifier,
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()
        event = sink.events[identifier][-1]
        assert isinstance(event, RunSucceededEvent), event
        return event

    first = asyncio.run(execute("native-1"))
    assert first.data.deferred_tool_call.tool_call_id == "install"
    assert all(call["action"] == "poll" for call in http_calls)
    pending.append({"id": "second", "content": "补充二：报告改为横版。"})
    request.session_snapshot = first.data.session_snapshot
    request.deferred_tool_results = DeferredToolResultsPayload(calls={"install": {"status": "completed"}})
    second = asyncio.run(execute("native-2"))
    assert second.data.deferred_tool_call is None
    assert model_calls == 2
    assert next(call for call in http_calls if call["backend_run_id"] == "native-2")["seen_ids"] == ["first"]
    assert http_calls[-1]["seen_ids"] == ["first", "second"]


@pytest.mark.parametrize("failed_boundary", ["poll", "seal"])
@pytest.mark.parametrize("failure", ["timeout", 429, 503])
def test_one_transient_followup_read_must_not_abort_an_otherwise_successful_task(monkeypatch, failed_boundary, failure):
    counts = {"model": 0, "business_tool": 0, "http": 0, "failure": 0}

    async def work():
        counts["business_tool"] += 1
        return "original work completed"

    async def stream(messages, info):
        counts["model"] += 1
        if counts["model"] == 1:
            yield {0: _call("work", {}, "original")}
        else:
            yield "任务已完成。"

    def transport(request):
        data = json.loads(request.content)
        counts["http"] += 1
        if data["action"] == failed_boundary and not counts["failure"]:
            counts["failure"] += 1
            if failure == "timeout":
                raise httpx.ReadTimeout("single transient endpoint timeout", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json={"messages": [], "sealed": data["action"] == "seal"})

    request, sink, _ = _setup(monkeypatch, stream, [Tool(work)])
    context = next(layer for layer in request.composition.layers if layer.name == "execution_context")
    context.config = {**dict(context.config), "workbench_run_id": "turn-1", "user_id": "account-1", "app_id": "app-1"}
    request.composition.layers.append(
        RunLayerSpec(
            name="workbench_followups",
            type="dify.workbench_followups",
            deps={"execution_context": "execution_context"},
        )
    )

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="audit-timeout",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(execute())
    terminal = sink.events["audit-timeout"][-1]
    assert isinstance(terminal, RunSucceededEvent), terminal
    assert counts["business_tool"] == 1
