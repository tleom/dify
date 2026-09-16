"""Exercise real SDK model boundaries, repeated provider call IDs and plan suspension."""

import asyncio
import json

import httpx
import pytest
from pydantic_ai import Tool

from dify_agent.protocol import DeferredToolResultsPayload, RunLayerSpec, RunSucceededEvent
from dify_agent.protocol.workbench_control import GoalState, TodoWrite, WorkbenchControlState
from dify_agent.runtime.runner import AgentRunRunner
from .test_workbench_activity import _call, _progress, _setup


def add_control(request):
    context = next(layer.config for layer in request.composition.layers if layer.name == "execution_context")
    context.workbench_run_id, context.user_id, context.app_id = "turn-1", "owner-1", "app-1"
    request.composition.layers.append(
        RunLayerSpec(
            name="workbench_control",
            type="dify.workbench_control",
            deps={"execution_context": "execution_context"},
            config={},
        )
    )


def test_get_goal_exposes_the_goal_revision_without_ambiguous_control_revision(monkeypatch):
    state = WorkbenchControlState(revision=17, goal=GoalState(id="current-goal", objective="核验", revision=3))
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: _call("get_goal", {}, "goal-read")}
        else:
            yield "已读取目标。"

    request, sink, _ = _setup(monkeypatch, stream)
    add_control(request)

    def transport(request):
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": {"content": ""}, "skills": [], "global_resources": {}})
        return httpx.Response(200, json={"state": state.model_dump(mode="json"), "control": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="goal-revision",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    assert isinstance(sink.events["goal-revision"][-1], RunSucceededEvent)
    returned = next(
        item
        for item in _progress(sink.events["goal-revision"], "tool")
        if item.tool_name == "get_goal" and item.stage == "returned"
    )
    assert returned.output["goal_id"] == "current-goal"
    assert returned.output["revision"] == 3


@pytest.mark.parametrize("mode", ["goal", "ordinary"])
def test_business_work_waits_for_an_initial_and_periodic_truthful_task_checkpoint(monkeypatch, mode):
    state = WorkbenchControlState(goal=GoalState(objective="核验多项材料") if mode == "goal" else None)
    calls = 0
    executed = []
    rejected_calls = set()
    updates = (
        {2: "in_progress", 8: "in_progress", 10: "completed"} if mode == "goal" else {6: "in_progress", 8: "completed"}
    )
    checkpoints = {1, 7} if mode == "goal" else {5}
    last_update = max(updates)

    async def work():
        executed.append(len(executed) + 1)
        return "已读取一部分材料，步骤尚未完成"

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        for message in messages:
            for part in message.parts:
                if "尚未执行" in str(getattr(part, "content", "")):
                    rejected_calls.add(part.tool_call_id)
        if calls in checkpoints:
            assert "TASK CHECKPOINT:" in info.instructions
        if calls in updates:
            yield {
                0: _call(
                    "todo_write",
                    {"todos": [{"content": "核验材料", "status": updates[calls]}]},
                    f"todo-{calls}",
                )
            }
        elif calls < last_update:
            yield {0: _call("work", {}, f"work-{calls}")}
        else:
            yield "已核验。"

    request, sink, _ = _setup(monkeypatch, stream, [Tool(work)])
    add_control(request)

    def transport(request):
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": {"content": ""}, "skills": [], "global_resources": {}})
        payload = json.loads(request.content)
        if payload["action"] == "todo_write":
            state.todos = TodoWrite.model_validate(payload["data"]).todos
        return httpx.Response(200, json={"state": state.model_dump(mode="json"), "control": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="task-checkpoint",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    assert isinstance(sink.events["task-checkpoint"][-1], RunSucceededEvent)
    assert executed == [1, 2, 3, 4, 5]
    assert rejected_calls == ({"work-1", "work-7"} if mode == "goal" else {"work-5"})


@pytest.mark.parametrize("continue_after", [False, True])
def test_manual_compaction_continues_same_run_only_when_instruction_was_supplied(monkeypatch, continue_after):
    state = WorkbenchControlState()
    phases, model_calls = [], []

    async def stream(messages, info):
        assert phases[-1] in {"compacted", "unchanged"}
        model_calls.append(messages)
        yield "继续指令已执行。"

    request, sink, _ = _setup(monkeypatch, stream)
    add_control(request)

    def transport(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": {"content": ""}, "skills": [], "global_resources": {}})
        if payload["action"] == "compact_result":
            phases.append(payload["data"]["phase"])
        return httpx.Response(
            200,
            json={
                "state": state.model_dump(mode="json"),
                "control": {"kind": "compact", "id": "compact-one", "continue_after": continue_after},
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="compact-one",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    terminal = sink.events["compact-one"][-1]
    assert isinstance(terminal, RunSucceededEvent), terminal
    assert phases == ["compacting", "unchanged"]
    assert len(model_calls) == int(continue_after)
    if continue_after:
        assert terminal.data.output == "继续指令已执行。"


def test_reused_provider_ids_update_distinct_steps_and_retry_the_same_http_operation(monkeypatch):
    state = WorkbenchControlState(goal=GoalState(id="goal", objective="验收", revision=1, phase="active"))
    calls = 0
    mutations = {}
    failed_once = False

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        assert "memory-fixture" in info.instructions
        assert "Do not batch completions at the end" in info.instructions
        if calls == 2:
            assert "[in_progress] 验证步骤" in info.instructions
        elif calls >= 3:
            assert "[completed] 验证步骤" in info.instructions
        if calls <= 2:
            yield {
                0: _call(
                    "todo_write",
                    {"todos": [{"content": "验证步骤", "status": "in_progress" if calls == 1 else "completed"}]},
                    "reused-id",
                )
            }
        elif calls == 3:
            yield {
                0: _call(
                    "update_goal",
                    {"goal_id": "goal", "revision": 1, "phase": "complete", "reason": "已验证"},
                    "reused-id",
                )
            }
        else:
            yield "验收完成。"

    request, sink, _ = _setup(monkeypatch, stream)
    add_control(request)

    def transport(request):
        nonlocal failed_once
        payload = json.loads(request.content)
        if request.url.path.endswith("/resources"):
            return httpx.Response(
                200, json={"memory": {"content": "memory-fixture"}, "skills": [], "global_resources": {}}
            )
        action = payload["action"]
        if action != "read":
            key = payload["request_key"]
            if key in mutations and mutations[key] != payload:
                return httpx.Response(409, json={"message": "状态更新编号已用于另一个操作"})
            mutations[key] = payload
            if action == "todo_write":
                state.todos = TodoWrite.model_validate(payload["data"]).todos
                if state.todos[0].status == "completed" and not failed_once:
                    failed_once = True
                    return httpx.Response(503)
            elif action == "update_goal":
                assert all(item.status == "completed" for item in state.todos)
                state.goal.phase = "complete"
        return httpx.Response(200, json={"state": state.model_dump(mode="json"), "control": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="control",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    assert isinstance(sink.events["control"][-1], RunSucceededEvent)
    assert state.goal.phase == "complete" and calls == 4
    assert len(mutations) == 3 and failed_once
    assert all(item.stage != "error" for item in _progress(sink.events["control"], "tool"))
    first = next(item for item in _progress(sink.events["control"], "tool") if item.tool_name == "todo_write")
    assert first.input == {"todos": [{"content": "验证步骤", "status": "in_progress"}]}


@pytest.mark.parametrize("approved", [False, True])
def test_complete_plan_is_deferred_and_resumes_with_the_explicit_decision(monkeypatch, approved):
    state = WorkbenchControlState()
    state.plan.active = True
    calls = 0
    plan = "# 文件整理计划\n\n1. 查看来源。\n2. 用户批准后生成报告。\n\n验证：核对内容和下载链接。"

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert "PLAN MODE:" in info.instructions
            yield {0: _call("exit_plan_mode", {"plan": plan}, "plan-call")}
        else:
            assert ("PLAN MODE:" in info.instructions) == (not approved)
            yield "开始执行。" if approved else "根据意见继续完善计划。"

    request, sink, _ = _setup(monkeypatch, stream)
    add_control(request)

    def transport(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": {"content": ""}, "skills": [], "global_resources": {}})
        if payload["action"] == "review_plan":
            state.plan.review = payload["data"]["plan"]
        return httpx.Response(200, json={"state": state.model_dump(mode="json"), "control": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="plan-first",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()
            terminal = sink.events["plan-first"][-1]
            assert isinstance(terminal, RunSucceededEvent)
            pending = terminal.data.deferred_tool_call
            assert pending is not None and pending.tool_name == "exit_plan_mode"
            assert pending.args["markdown"] == plan
            assert state.plan.review == plan and calls == 1
            request.session_snapshot = terminal.data.session_snapshot
            state.plan.active = not approved
            state.plan.review = None
            request.deferred_tool_results = DeferredToolResultsPayload(
                calls={
                    "plan-call": {
                        "action": "approve" if approved else "keep_planning",
                        "answers": {"feedback": "核验来源"},
                    }
                }
            )
            await AgentRunRunner(
                run_id="plan-resume",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    assert isinstance(sink.events["plan-resume"][-1], RunSucceededEvent)
    assert calls == 2
