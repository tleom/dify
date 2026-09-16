"""Exercise real SDK model boundaries, repeated provider call IDs and plan suspension."""

import asyncio
import json

import httpx
import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart

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
def test_business_work_needs_no_initial_or_periodic_task_list(monkeypatch, mode):
    state = WorkbenchControlState(goal=GoalState(objective="核验多项材料") if mode == "goal" else None)
    calls = 0
    executed = []
    rejected_calls = set()

    async def work():
        executed.append(len(executed) + 1)
        return "已读取一部分材料，步骤尚未完成"

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        for message in messages:
            for part in message.parts:
                if isinstance(part, (RetryPromptPart, ToolReturnPart)) and "尚未执行" in str(part.content):
                    rejected_calls.add(part.tool_call_id)
        assert "TASK CHECKPOINT:" not in info.instructions
        if calls <= 9:
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
    assert executed == list(range(1, 10))
    assert rejected_calls == set()
    assert state.todos == []


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
        assert "Optional" in info.instructions or "optional" in info.instructions
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
            if approved:
                assert plan in info.instructions
                yield "开始执行。"
            else:
                yield {0: _call("exit_plan_mode", {"plan": plan + "\n新增来源核验。"}, "revised-plan")}

    request, sink, _ = _setup(monkeypatch, stream)
    add_control(request)

    def transport(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": {"content": ""}, "skills": [], "global_resources": {}})
        if payload["action"] == "review_plan":
            state.plan.submit(payload["data"]["plan"], "turn-1")
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
            assert pending.metadata["plan_version"] == 1
            state.plan.answer(run_id="turn-1", version=1, plan=plan, approve=approved)
            request.deferred_tool_results = DeferredToolResultsPayload(
                calls={
                    "plan-call": {
                        "action": "approve" if approved else "keep_planning",
                        "answers": {"feedback": "" if approved else "核验来源"},
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
    if not approved:
        assert state.plan.active and state.plan.version == 2
        assert sink.events["plan-resume"][-1].data.deferred_tool_call.args["markdown"].endswith("新增来源核验。")


def test_planning_hides_execution_tools_and_blocks_invented_calls_in_review_batch(monkeypatch):
    state = WorkbenchControlState()
    state.plan.active = True
    executed, offered = [], []
    plan = "# 实施方案\n\n核对来源后生成文档；完成后验证。"

    async def shell():
        executed.append("shell")
        return "已写入"

    async def stream(messages, info):
        offered.extend(tool.name for tool in info.function_tools)
        yield {
            0: _call("shell", {}, "forbidden-write"),
            1: _call("todo_write", {"todos": [{"content": "实施", "status": "in_progress"}]}, "forbidden-todo"),
            2: _call("exit_plan_mode", {"plan": plan}, "review"),
        }

    request, sink, _ = _setup(monkeypatch, stream, [Tool(shell)])
    add_control(request)

    def transport(request):
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": {}, "skills": [], "global_resources": {}})
        payload = json.loads(request.content)
        assert payload["action"] != "todo_write"
        if payload["action"] == "review_plan":
            state.plan.submit(payload["data"]["plan"], "turn-1")
        return httpx.Response(200, json={"state": state.model_dump(mode="json"), "control": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="plan-fence",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    terminal = sink.events["plan-fence"][-1]
    assert isinstance(terminal, RunSucceededEvent), terminal
    assert terminal.data.deferred_tool_call.tool_name == "exit_plan_mode"
    assert executed == [] and state.todos == [] and state.plan.active
    assert "shell" not in offered and "todo_write" not in offered and "update_memory" not in offered
    assert "plan_inspect" in offered and "exit_plan_mode" in offered


@pytest.mark.parametrize("with_files", [False, True])
def test_final_text_cannot_skip_plan_review_or_leak_to_stream(monkeypatch, with_files):
    state = WorkbenchControlState()
    state.plan.active = True
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield "我将直接开始生成文件。"
        else:
            yield "已完成调查，提交完整方案供你审阅。"
            yield {0: _call("exit_plan_mode", {"plan": "# 处理方案\n核对、实施并验证。"}, "review")}

    request, sink, _ = _setup(monkeypatch, stream)
    add_control(request)
    if with_files:
        request.composition.layers.append(
            RunLayerSpec(
                name="workbench_files",
                type="dify.workbench_files",
                deps={"execution_context": "execution_context"},
                config={},
            )
        )

    def transport(request):
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": {}, "skills": [], "global_resources": {}})
        payload = json.loads(request.content)
        if payload["action"] == "review_plan":
            state.plan.submit(payload["data"]["plan"], "turn-1")
        return httpx.Response(200, json={"state": state.model_dump(mode="json"), "control": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="plan-output",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    terminal = sink.events["plan-output"][-1]
    assert isinstance(terminal, RunSucceededEvent), terminal
    assert terminal.data.deferred_tool_call.tool_name == "exit_plan_mode" and calls == 2
    visible = [item.text for item in _progress(sink.events["plan-output"], "text")]
    assert visible == ["已完成调查，提交完整方案供你审阅。"]
    assert not any(
        "我将直接开始生成文件。" in event.model_dump_json()
        for event in sink.events["plan-output"]
        if event.type == "workbench_activity"
    )


@pytest.mark.parametrize("planning", [False, True])
def test_external_environment_tool_obeys_plan_boundary(monkeypatch, planning):
    state = WorkbenchControlState()
    state.plan.active = planning
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        assert ("update_shared_environment" in {tool.name for tool in info.function_tools}) is (not planning)
        if calls == 1:
            yield {0: _call("update_shared_environment", {"python": ["pandas"], "reason": "inspect"}, "install")}
        else:
            assert any(
                isinstance(part, RetryPromptPart) and part.tool_name == "update_shared_environment"
                for message in messages
                for part in message.parts
            )
            yield {0: _call("exit_plan_mode", {"plan": "# 方案\n批准后处理。"}, "review")}

    request, sink, _ = _setup(monkeypatch, stream)
    add_control(request)
    request.composition.layers.append(
        RunLayerSpec(name="workbench_environment", type="dify.workbench_environment", config={})
    )

    def transport(request):
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": {}, "skills": [], "global_resources": {}})
        payload = json.loads(request.content)
        if payload["action"] == "review_plan":
            state.plan.submit(payload["data"]["plan"], "turn-1")
        return httpx.Response(200, json={"state": state.model_dump(mode="json"), "control": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="environment-plan",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    terminal = sink.events["environment-plan"][-1]
    assert isinstance(terminal, RunSucceededEvent), terminal
    assert terminal.data.deferred_tool_call.tool_name == ("exit_plan_mode" if planning else "update_shared_environment")
    assert calls == (2 if planning else 1)


def test_structured_output_cannot_complete_an_unreviewed_plan(monkeypatch):
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from dify_agent.layers.dify_plugin.llm_layer import DifyPluginLLMLayer
    from dify_agent.layers.output.configs import DifyOutputLayerConfig
    from dify_agent.protocol import RunFailedEvent
    from dify_agent.runtime.event_sink import InMemoryRunEventSink
    from .test_runner import SequenceOutputTestModel, _request

    model = SequenceOutputTestModel(outputs=[{"summary": "ready"}])
    monkeypatch.setattr(DifyPluginLLMLayer, "get_model", lambda *_a, **_k: model)
    request = _request(
        output_config=DifyOutputLayerConfig(
            json_schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            }
        )
    )
    add_control(request)
    sink = InMemoryRunEventSink()
    state = WorkbenchControlState()
    state.plan.active = True

    def transport(request):
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": {}, "skills": [], "global_resources": {}})
        return httpx.Response(200, json={"state": state.model_dump(mode="json"), "control": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="structured-plan",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    with pytest.raises(UnexpectedModelBehavior, match="maximum output retries"):
        asyncio.run(scenario())
    assert isinstance(sink.events["structured-plan"][-1], RunFailedEvent)
    assert not any(isinstance(event, RunSucceededEvent) for event in sink.events["structured-plan"])
    assert state.plan.active and model.request_count > 1


@pytest.mark.parametrize("resolution", ["submitted", "cancelled", "timeout"])
def test_clarification_resolution_keeps_planning_and_requests_full_review(monkeypatch, resolution):
    state = WorkbenchControlState()
    state.plan.active = True
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        assert "PLAN MODE:" in info.instructions
        assert "todo_write" not in {tool.name for tool in info.function_tools}
        if calls == 1:
            yield {
                0: _call(
                    "ask_human",
                    {
                        "question": "需要核对哪些来源？",
                        "fields": [{"name": "scope", "type": "paragraph", "label": "来源", "required": False}],
                    },
                    "clarification",
                )
            }
        else:
            yield {0: _call("exit_plan_mode", {"plan": "# 核验方案\n确认来源后核对文件；批准后实施。"}, "review")}

    request, sink, _ = _setup(monkeypatch, stream)
    add_control(request)
    request.composition.layers.append(RunLayerSpec(name="ask_human", type="dify.ask_human", config={}))

    def transport(request):
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": {}, "skills": [], "global_resources": {}})
        value = json.loads(request.content)
        if value["action"] == "review_plan":
            state.plan.submit(value["data"]["plan"], "turn-1")
        return httpx.Response(200, json={"state": state.model_dump(mode="json"), "control": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="clarify",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()
            first = sink.events["clarify"][-1]
            assert isinstance(first, RunSucceededEvent), first
            assert first.data.deferred_tool_call.tool_name == "ask_human"
            request.session_snapshot = first.data.session_snapshot
            request.deferred_tool_results = DeferredToolResultsPayload(
                calls={
                    "clarification": {
                        "status": resolution,
                        "values": {"scope": "附件"} if resolution == "submitted" else {},
                        "action": None,
                    }
                }
            )
            await AgentRunRunner(
                run_id="clarified",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    terminal = sink.events["clarified"][-1]
    assert isinstance(terminal, RunSucceededEvent), terminal
    assert state.plan.active and state.plan.approved is None
    assert terminal.data.deferred_tool_call.tool_name == "exit_plan_mode" and calls == 2


def test_plan_inspection_returns_rendered_images_through_real_sdk_tool_result(monkeypatch):
    state = WorkbenchControlState()
    state.plan.active = True
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: _call("plan_inspect", {"script": "python render.py", "preview_paths": ["/tmp/page.png"]}, "inspect")
            }
        else:
            assert "image/png" in str(messages)
            yield {0: _call("exit_plan_mode", {"plan": "# 核验方案\n已检查预览；批准后生成。"}, "review")}

    request, sink, _ = _setup(monkeypatch, stream)
    add_control(request)

    def transport(request):
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"memory": {}, "skills": [], "global_resources": {}})
        value = json.loads(request.content)
        if request.url.path.endswith("/plan/inspect"):
            assert value["workbench_run_id"] == "turn-1"
            assert value["preview_paths"] == ["/tmp/page.png"]
            return httpx.Response(
                200,
                json={
                    "output": "rendered",
                    "exit_code": 0,
                    "previews": [
                        {
                            "path": "/tmp/page.png",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1ioAAAAASUVORK5CYII=",
                        }
                    ],
                },
            )
        if value["action"] == "review_plan":
            state.plan.submit(value["data"]["plan"], "turn-1")
        return httpx.Response(200, json={"state": state.model_dump(mode="json"), "control": None})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="inspect-image",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(scenario())
    terminal = sink.events["inspect-image"][-1]
    assert isinstance(terminal, RunSucceededEvent), terminal
    assert terminal.data.deferred_tool_call.tool_name == "exit_plan_mode" and calls == 2
