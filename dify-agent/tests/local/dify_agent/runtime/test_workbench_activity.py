"""Real Agenton/Pydantic execution and serialization; the provider is deterministic."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai import ModelRetry, Tool
from pydantic_ai.messages import ToolReturn, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from dify_agent.layers.dify_plugin.configs import DifyPluginToolConfig, DifyPluginToolsLayerConfig
from dify_agent.layers.dify_core_tools.configs import DifyCoreToolConfig
from dify_agent.layers.dify_plugin.llm_layer import DifyPluginLLMLayer
from dify_agent.layers.dify_plugin.tools_layer import DifyPluginToolsLayer
from dify_agent.layers.workbench_activity import WorkbenchActivityConfig, WorkbenchActivityState
from dify_agent.protocol import (
    RUN_EVENT_ADAPTER,
    DeferredToolResultsPayload,
    RunLayerSpec,
    RunSucceededEvent,
    WorkbenchActivityRunEvent,
)
from dify_agent.runtime.event_sink import InMemoryRunEventSink
from dify_agent.runtime.runner import AgentRunRunner

from .test_runner import _request


def _call(name, args, identifier):
    return DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=identifier)


def _setup(monkeypatch, stream, tools=()):
    monkeypatch.setattr(DifyPluginLLMLayer, "get_model", lambda *args, **kwargs: FunctionModel(stream_function=stream))

    async def get_tools(*args, **kwargs):
        return list(tools)

    monkeypatch.setattr(DifyPluginToolsLayer, "get_tools", get_tools)
    request = _request(include_history=True)
    request.composition.layers.append(
        RunLayerSpec(
            name="workbench_activity",
            type="dify.workbench_activity",
            config=WorkbenchActivityConfig(workbench_run_id="turn-1"),
        )
    )
    if tools:
        request.composition.layers.append(
            RunLayerSpec(
                name="tools",
                type="dify.plugin.tools",
                deps={"execution_context": "execution_context"},
                config=DifyPluginToolsLayerConfig(
                    tools=[
                        DifyPluginToolConfig(
                            plugin_id="test/tools", provider="test", tool_name=tool.name, credential_type="unauthorized"
                        )
                        for tool in tools
                    ]
                ),
            )
        )
    sink = InMemoryRunEventSink()

    async def execute(identifier="native-1"):
        async with httpx.AsyncClient() as client:
            await AgentRunRunner(
                run_id=identifier,
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()
        events = sink.events[identifier]
        assert isinstance(events[-1], RunSucceededEvent), events[-1]
        for event in events:
            assert RUN_EVENT_ADAPTER.validate_json(RUN_EVENT_ADAPTER.dump_json(event)) == event
        return events

    return request, sink, execute


def _progress(events, kind):
    return [event.data for event in events if isinstance(event, WorkbenchActivityRunEvent) and event.data.kind == kind]


def _state(events):
    layer = next(layer for layer in events[-1].data.session_snapshot.layers if layer.name == "workbench_activity")
    return WorkbenchActivityState.model_validate(layer.runtime_state)


def test_applied_file_diff_metadata_reaches_ui_without_echoing_to_model(monkeypatch):
    calls = 0
    applied = [{"oldText": "header\nold\ntail\n", "newText": "header\nnew\ntail\n"}]

    async def file_edit(path: str, old_text: str, new_text: str):
        return ToolReturn(
            return_value={"path": path, "operation": "edit", "bytes": 16}, metadata={"workbench_file_diffs": applied}
        )

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: _call("file_edit", {"path": "note.txt", "old_text": "old", "new_text": "new"}, "edit")}
        else:
            part = next(
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_name == "file_edit"
            )
            assert part.content == {"path": "note.txt", "operation": "edit", "bytes": 16}
            assert part.metadata["workbench_file_diffs"] == applied
            yield "已修改。"

    _, _, execute = _setup(monkeypatch, stream, [Tool(file_edit)])
    events = asyncio.run(execute())
    returned = next(
        item for item in _progress(events, "tool") if item.stage == "returned" and item.tool_name == "file_edit"
    )
    assert returned.output["diffs"] == applied


def test_missing_title_is_requested_at_next_model_boundary_without_replaying_work(monkeypatch):
    calls = 0
    executed = []

    async def work():
        executed.append("work")
        return "done"

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: _call("work", {}, "original")}
        elif calls == 2:
            assert "current tool group has no purpose title" in info.instructions
            yield {0: _call("report_activity", {"action": "begin", "title": "重新生成并核验公式"}, "late-title")}
        elif calls == 3:
            assert "current tool group has no purpose title" not in info.instructions
            yield {0: _call("report_activity", {"action": "close", "title": "核对生成文件中的公式"}, "close-title")}
        else:
            yield "公式已核对。"

    _, _, execute = _setup(monkeypatch, stream, [Tool(work)])
    events = asyncio.run(execute())
    assert executed == ["work"]
    assert [item.title for item in _progress(events, "activity")] == ["重新生成并核验公式", "核对生成文件中的公式"]


def test_reused_provider_ids_keep_distinct_commands_and_model_goal_updates(monkeypatch):
    requests = 0

    async def work(attempt: int):
        return {"exit_code": 1 if attempt == 1 else 0, "done": True, "job_id": f"job-{attempt}"}

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: _call("report_activity", {"action": "begin", "title": "检查解析依赖以读取文件"}, "report_activity"),
                1: _call("work", {"attempt": 1}, "work"),
            }
        elif requests == 2:
            yield {
                0: _call("report_activity", {"action": "update", "goal": "重试读取以验证依赖恢复"}, "report_activity"),
                1: _call("work", {"attempt": 2}, "work"),
            }
        elif requests == 3:
            yield {
                0: _call(
                    "report_activity", {"action": "close", "goal": "重新读取成功，已验证依赖恢复"}, "report_activity"
                )
            }
        else:
            yield "读取验证成功。"

    _, _, execute = _setup(monkeypatch, stream, [Tool(work)])
    events = asyncio.run(execute())
    tools = _progress(events, "tool")
    starts = [event for event in tools if event.stage == "started"]
    assert len(starts) == 2
    assert len({event.call_id for event in starts}) == 2
    assert [event.input for event in starts] == [{"attempt": 1}, {"attempt": 2}]
    returns = [event for event in tools if event.stage != "started"]
    assert [event.stage for event in returns] == ["error", "returned"]
    assert [event.call_id for event in returns] == [event.call_id for event in starts]
    activities = _progress(events, "activity")
    assert len({event.activity_id for event in activities}) == 1
    assert [event.revision for event in activities] == [1, 2, 3]
    assert activities[-1].title == "检查解析依赖以读取文件"
    assert activities[-1].goal == "重新读取成功，已验证依赖恢复"


def test_malformed_shell_calls_do_not_execute_and_recover_from_complete_argument_wrapper(monkeypatch):
    requests = 0
    executed = []

    async def shell_run(script: str):
        executed.append(script)
        return {"done": True, "exit_code": 0}

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests <= 2:
            yield {0: _call("shell_run", {"arguments": '{"script":"incomplete'}, "shell_run")}
        elif requests == 3:
            yield {0: _call("shell_run", {"arguments": json.dumps({"script": "safe command"})}, "shell_run")}
        else:
            yield "已完成。"

    _, _, execute = _setup(monkeypatch, stream, [Tool(shell_run)])
    events = asyncio.run(execute())
    assert executed == ["safe command"]
    returned = [item for item in _progress(events, "tool") if item.stage != "started"]
    assert [item.stage for item in returned] == ["error", "error", "returned"]
    assert len({item.call_id for item in returned}) == 3


@pytest.mark.parametrize("coalesce", [True, False])
def test_purpose_binding_preserves_parallelism_and_two_barriers(monkeypatch, coalesce):
    requests = 0
    running = 0
    peak = 0

    async def work(name: str):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01 if name == "two" else 0.025)
        running -= 1
        return {"name": name, "status": "ok"}

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: _call("report_activity", {"action": "begin", "title": "定位文档读取失败的原因"}, "r1"),
                1: _call("work", {"name": "one"}, "one"),
                2: _call("work", {"name": "two"}, "two"),
                3: _call("report_activity", {"action": "begin", "title": "核验文档格式要求"}, "r2"),
                4: _call("work", {"name": "three"}, "three"),
            }
        elif requests == 2:
            yield {
                0: _call(
                    "report_activity",
                    {
                        "action": "close",
                        "title": "已核验文档格式要求",
                        "evidence_call_ids": ["three"],
                    },
                    "close",
                )
            }
        else:
            yield "已完成核验。"

    request, sink, execute = _setup(monkeypatch, stream, [Tool(work)])

    # The actual runner's coalescing switch must not change purpose boundaries.
    async def run():
        async with httpx.AsyncClient() as client:
            await AgentRunRunner(
                run_id="native-1",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
                stream_text_delta_coalescing_enabled=coalesce,
            ).run()
        return sink.events["native-1"]

    events = asyncio.run(run())
    assert isinstance(events[-1], RunSucceededEvent)
    for event in events:
        assert RUN_EVENT_ADAPTER.validate_json(RUN_EVENT_ADAPTER.dump_json(event)) == event
    activities = _progress(events, "activity")
    starts = {item.tool_call_id: item for item in _progress(events, "tool") if item.stage == "started"}
    assert len(activities) == 3
    assert starts["one"].activity_id == starts["two"].activity_id == activities[0].activity_id
    assert starts["three"].activity_id == activities[1].activity_id != activities[0].activity_id
    assert peak == 2
    assert set(starts) == {"one", "two", "three"}
    assert activities[-1].revision == 2
    assert "".join(item.text for item in _progress(events, "text")) == "已完成核验。"


def test_environment_continuation_keeps_original_activity_and_call_identity(monkeypatch):
    requests = 0

    async def verify():
        return {"read_succeeded": True}

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: _call("report_activity", {"action": "begin", "title": "安装缺失依赖以恢复文档读取"}, "begin"),
                1: _call("update_shared_environment", {"reason": "恢复 Word 读取", "python": ["python-docx"]}, "env"),
            }
        elif requests == 2:
            yield {
                0: _call("report_activity", {"action": "update", "title": "重新读取文档并验证修复结果"}, "update"),
                1: _call("verify", {}, "verify"),
                2: _call(
                    "report_activity",
                    {"action": "close", "title": "已补齐依赖并恢复文档读取", "evidence_call_ids": ["verify"]},
                    "close",
                ),
            }
        else:
            yield "文档已读取。"

    request, sink, execute = _setup(monkeypatch, stream, [Tool(verify)])
    request.composition.layers.append(
        RunLayerSpec(name="workbench_environment", type="dify.workbench_environment", config={})
    )
    first = asyncio.run(execute())
    assert first[-1].data.deferred_tool_call.tool_call_id == "env"
    assert _state(first).calls["native-1:env"].state == "running"
    request.session_snapshot = first[-1].data.session_snapshot
    request.deferred_tool_results = DeferredToolResultsPayload(calls={"env": {"status": "completed"}})
    second = asyncio.run(execute("native-2"))
    returned = next(item for item in _progress(second, "tool") if item.tool_call_id == "env")
    assert returned.call_id == "native-1:env"
    assert returned.stage == "returned"
    assert {item.activity_id for item in _progress(first + second, "activity")} == {returned.activity_id}
    assert _progress(second, "activity")[-1].action == "close"
    assert _state(second).calls["native-2:verify"].activity_id == returned.activity_id


def test_background_output_does_not_allow_premature_close(monkeypatch):
    requests = 0

    async def shell_run():
        return {"job_id": "job", "done": False, "stdout": "preparing"}

    async def shell_wait():
        return {"job_id": "job", "done": True, "exit_code": 0, "stdout": "ready"}

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: _call("report_activity", {"action": "begin", "title": "读取文档以核验内容"}, "begin"),
                1: _call("shell_run", {}, "run"),
            }
        elif requests == 2:
            yield {
                0: _call("report_activity", {"action": "close", "title": "已读取文档"}, "too-soon"),
                1: _call("shell_wait", {}, "wait"),
            }
        elif requests == 3:
            yield {
                0: _call(
                    "report_activity",
                    {"action": "close", "title": "已读取文档并核验内容", "evidence_call_ids": ["wait"]},
                    "close",
                )
            }
        else:
            yield "核验完成。"

    _, _, execute = _setup(monkeypatch, stream, [Tool(shell_run), Tool(shell_wait)])
    events = asyncio.run(execute())
    activities = _progress(events, "activity")
    assert [activity.action for activity in activities] == ["begin", "close"]
    assert activities[-1].title == "已读取文档并核验内容"
    assert _state(events).jobs == {"job": True}


def test_compacted_environment_resume_preserves_activity_and_call_identity(monkeypatch):
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter,
        ModelRequest,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    requests = 0
    summaries = 0
    activity_id = ""

    async def work(attempt: int):
        return "validation data " * 2_000 if attempt < 14 else {"verified": True}

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: _call("report_activity", {"action": "begin", "title": "验证压缩后读取"}, "begin")}
        elif requests < 13:
            yield {0: _call("work", {"attempt": requests}, f"work-{requests}")}
        elif requests == 13:
            yield {0: _call("update_shared_environment", {"reason": "恢复读取", "python": ["python-docx"]}, "env")}
        elif requests == 14:
            report = next(tool for tool in info.function_tools if tool.name == "report_activity")
            assert activity_id in report.description
            yield {
                0: _call("report_activity", {"action": "update", "goal": "压缩后继续核验"}, "update"),
                1: _call("work", {"attempt": 14}, "verify"),
            }
        elif requests == 15:
            yield {0: _call("report_activity", {"action": "close"}, "close")}
        else:
            yield "验证完成。"

    def summarize(messages, info):
        nonlocal summaries
        summaries += 1
        return ModelResponse(parts=[TextPart("Earlier reads completed; environment update is ready for verification.")])

    request, _, execute = _setup(monkeypatch, stream, [Tool(work)])
    monkeypatch.setattr(
        DifyPluginLLMLayer,
        "get_model",
        lambda *args, **kwargs: FunctionModel(function=summarize, stream_function=stream),
    )
    request.composition.layers.append(
        RunLayerSpec(name="workbench_environment", type="dify.workbench_environment", config={})
    )
    llm = next(layer for layer in request.composition.layers if layer.type == "dify.plugin.llm")
    config = llm.config.model_dump() if hasattr(llm.config, "model_dump") else dict(llm.config)
    config["context_window_tokens"] = 1_000
    config["model_settings"] = None
    llm.config = config
    first = asyncio.run(execute())
    first_state = _state(first)
    activity_id = first_state.current_id
    assert first_state.calls["native-1:env"].state == "running"
    request.session_snapshot = first[-1].data.session_snapshot
    # Model a long saved conversation without provider usage anchors. The native
    # compactor must actually replace older messages, including the begin report.
    history = next(layer for layer in request.session_snapshot.layers if layer.name == "history")
    prefix = []
    for index in range(30):
        prefix.extend(
            [
                ModelRequest(parts=[UserPromptPart(f"Earlier request {index}: " + "context " * 200)]),
                ModelResponse(parts=[TextPart("Earlier response " + "verified " * 200)]),
            ]
        )
    history.runtime_state["messages"] = [
        *ModelMessagesTypeAdapter.dump_python(prefix, mode="json"),
        *history.runtime_state["messages"],
    ]
    for message in history.runtime_state["messages"]:
        if message.get("kind") == "response":
            message["usage"] = {"input_tokens": 0, "output_tokens": 0}
    request.deferred_tool_results = DeferredToolResultsPayload(calls={"env": {"status": "completed"}})
    second = asyncio.run(execute("native-2"))
    assert summaries > 0
    from .test_runner import _history_messages_from_snapshot

    assert not any(
        isinstance(part, ToolCallPart | ToolReturnPart) and part.tool_call_id == "begin"
        for message in _history_messages_from_snapshot(second[-1].data.session_snapshot)
        for part in message.parts
    )
    assert _state(second).current_id == activity_id
    assert _state(second).calls["native-1:env"].state == "returned"
    assert _state(second).calls["native-2:verify"].activity_id == activity_id
    assert _progress(second, "activity")[-1].action == "close"


def test_disabled_reporting_keeps_business_events_for_existing_journal_runs(monkeypatch):
    requests = 0

    async def work():
        return "done"

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        assert "report_activity" not in {tool.name for tool in info.function_tools}
        assert "report_activity" not in (info.instructions or "")
        if requests == 1:
            yield {0: _call("work", {}, "work")}
        else:
            yield "已完成。"

    request, _, execute = _setup(monkeypatch, stream, [Tool(work)])
    activity = next(layer for layer in request.composition.layers if layer.name == "workbench_activity")
    activity.config = WorkbenchActivityConfig(workbench_run_id="turn-1", enabled=False)
    events = asyncio.run(execute())
    assert not _progress(events, "activity")
    assert [item.stage for item in _progress(events, "tool")] == ["started", "returned"]
    assert "".join(item.text for item in _progress(events, "text")) == "已完成。"


@pytest.mark.parametrize("wrapped_failure", [True, False])
def test_tool_failure_metadata_preserves_the_original_observation(monkeypatch, wrapped_failure):
    requests = 0
    observation = "tool invoke error: provider rejected the request"

    async def work():
        return ToolReturn(return_value=observation, metadata={"is_error": True}) if wrapped_failure else observation

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: _call("work", {}, "work")}
        else:
            parts = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            assert parts[-1].content == observation
            yield "已读取工具结果。"

    _, _, execute = _setup(monkeypatch, stream, [Tool(work)])
    events = asyncio.run(execute())
    calls = _progress(events, "tool")
    assert [item.stage for item in calls] == ["started", "error" if wrapped_failure else "returned"]
    assert calls[-1].output == observation
    assert next(iter(_state(events).calls.values())).state == ("error" if wrapped_failure else "returned")


def test_invalid_report_does_not_abort_or_repeat_business_tools(monkeypatch):
    requests = 0
    calls = 0

    async def work():
        nonlocal calls
        calls += 1
        return "done"

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: _call("report_activity", {"action": "not-an-action", "title": "bad"}, "invalid"),
                1: _call("work", {}, "work"),
                2: _call(
                    "report_activity",
                    {"action": "update", "activity_id": "another-run", "title": "跨任务标题"},
                    "foreign",
                ),
            }
        else:
            yield "done"

    request, sink, execute = _setup(monkeypatch, stream, [Tool(work)])
    events = asyncio.run(execute())
    assert calls == 1 and requests == 2
    assert _progress(events, "activity") == []
    assert len([item for item in _progress(events, "tool") if item.stage == "started"]) == 1


@pytest.mark.parametrize("failure_phase", ["validation", "execution"])
def test_retries_record_validation_errors_without_executing_invalid_calls(monkeypatch, failure_phase):
    requests = 0
    executed = []

    async def work(attempt: int):
        executed.append(attempt)
        if attempt == 1:
            raise ModelRetry("Retry after the failed execution")
        return "done"

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            args = {"attempt": "invalid" if failure_phase == "validation" else 1}
            yield {0: _call("work", args, "work")}
        elif requests == 2:
            assert any(
                isinstance(part, ToolReturnPart) and part.outcome == "failed"
                for message in messages
                for part in message.parts
            )
            yield {0: _call("work", {"attempt": 2}, "work")}
        else:
            yield "done"

    _, _, execute = _setup(monkeypatch, stream, [Tool(work)])
    events = asyncio.run(execute())
    expected_attempts = [2] if failure_phase == "validation" else [1, 2]
    expected_stages = ["started", "error", "started", "returned"]
    assert executed == expected_attempts
    tools = _progress(events, "tool")
    assert [item.stage for item in tools] == expected_stages
    assert [
        json.loads(item.input) if isinstance(item.input, str) else item.input
        for item in tools
        if item.stage == "started"
    ] == [{"attempt": attempt} for attempt in (["invalid", 2] if failure_phase == "validation" else [1, 2])]
    assert len(_state(events).calls) == 2


def test_reporting_only_loop_is_bounded_and_does_not_create_tool_rows(monkeypatch):
    requests = 0

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if any(tool.name == "report_activity" for tool in info.function_tools):
            yield {
                0: _call(
                    "report_activity", {"action": "begin", "title": f"准备核对第 {requests} 项资料"}, f"r{requests}"
                )
            }
        else:
            yield "无需执行工具。"

    request, sink, execute = _setup(monkeypatch, stream)
    events = asyncio.run(execute())
    assert requests == 5
    assert len(_progress(events, "activity")) == 4
    assert _progress(events, "tool") == []


def test_new_logical_run_resets_activity_state(monkeypatch):
    requests = 0

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: _call("report_activity", {"action": "begin", "title": "检查第一轮材料"}, "r1")}
        else:
            yield "done"

    request, sink, execute = _setup(monkeypatch, stream)
    first = asyncio.run(execute())
    request.session_snapshot = first[-1].data.session_snapshot
    request.composition.layers[-1].config = WorkbenchActivityConfig(workbench_run_id="turn-2")
    request.rebuild_layers = True
    second = asyncio.run(execute("native-2"))
    assert _state(second).workbench_run_id == "turn-2"
    assert _state(second).current_id is None
    assert not _state(second).calls


def test_soft_knowledge_error_remains_visible_without_aborting_run(monkeypatch):
    requests = 0

    async def knowledge_base_search():
        return "Knowledge base search is temporarily unavailable; continue with available sources."

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: _call("knowledge_base_search", {}, "search")}
        else:
            yield "检索暂不可用，继续核对已有材料。"

    _, _, execute = _setup(monkeypatch, stream, [Tool(knowledge_base_search)])
    events = asyncio.run(execute())
    assert [item.stage for item in _progress(events, "tool")] == ["started", "error"]
    assert _state(events).calls["native-1:search"].state == "error"


@pytest.mark.parametrize("source", ["plugin", "core"])
@pytest.mark.parametrize("field", ["tool_name", "name"])
def test_configured_tools_cannot_claim_the_activity_name(source, field):
    config = {"tool_name": "business_tool", field: "report_activity"}
    with pytest.raises(ValidationError, match="reserved"):
        if source == "plugin":
            DifyPluginToolConfig.model_validate(
                {
                    **config,
                    "plugin_id": "test/tools",
                    "provider": "test",
                    "credential_type": "unauthorized",
                }
            )
        else:
            DifyCoreToolConfig.model_validate({**config, "provider_type": "api", "provider_id": "test"})


def test_workbench_files_tool_is_bound_to_server_identity_and_delivers_exact_urls(monkeypatch):
    requests = 0
    preview = "https://files.example.test/files/workbench/signed/chart.png?mode=preview"
    download = "https://files.example.test/files/workbench/signed/chart.png?mode=download"

    async def stream(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            tool = next(tool for tool in info.function_tools if tool.name == "workbench_files")
            assert set(tool.parameters_json_schema["properties"]) == {"path"}
            yield {0: _call("workbench_files", {"path": "chart.png"}, "files-1")}
        else:
            result = next(
                part
                for message in reversed(messages)
                for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_name == "workbench_files"
            )
            item = json.loads(result.content)["entries"][0]
            assert item["preview_url"] == preview and item["download_url"] == download
            yield f"文件已在文件空间可见，可打开查看。[下载]({item['download_url']})\n![图表]({item['preview_url']})"

    request, sink, _ = _setup(monkeypatch, stream)
    context = next(layer.config for layer in request.composition.layers if layer.name == "execution_context")
    context.workbench_run_id = "turn-1"
    context.user_id = "owner-1"
    context.app_id = "app-1"
    request.composition.layers.append(
        RunLayerSpec(
            name="workbench_files",
            type="dify.workbench_files",
            deps={"execution_context": "execution_context"},
            config={},
        )
    )

    def transport(request):
        assert request.url.path == "/inner/api/agent/workbench/files"
        assert json.loads(request.content) == {
            "tenant_id": "tenant-1",
            "account_id": "owner-1",
            "app_id": "app-1",
            "workbench_run_id": "turn-1",
            "path": "chart.png",
        }
        return httpx.Response(
            200,
            json={
                "directory": "conversations/chat-1",
                "complete": True,
                "entries": [{"name": "chart.png", "preview_url": preview, "download_url": download}],
            },
        )

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await AgentRunRunner(
                run_id="files-run",
                request=request,
                sink=sink,
                plugin_daemon_http_client=client,
                dify_api_http_client=client,
            ).run()

    asyncio.run(execute())
    events = sink.events["files-run"]
    assert isinstance(events[-1], RunSucceededEvent), events[-1]
    assert requests == 2
    texts = "".join(item.text for item in _progress(events, "text"))
    assert preview in texts and download in texts
