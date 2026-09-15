from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.test import TestModel

from dify_agent.protocol.schemas import RUN_EVENT_ADAPTER
from dify_agent.runtime.compaction import build_compaction_capability
from dify_agent.runtime.context_status import WorkbenchContextStatus
from dify_agent.runtime.event_sink import InMemoryRunEventSink


def test_native_compaction_emits_lifecycle_and_keeps_summary_in_history():
    history = []
    for index in range(30):
        history.extend(
            [
                ModelRequest(parts=[UserPromptPart(f"user-{index}-" + "u" * 120)]),
                ModelResponse(parts=[TextPart("a" * 120)], model_name="test"),
            ]
        )
    sink = InMemoryRunEventSink()
    capability = WorkbenchContextStatus(
        compaction=build_compaction_capability(context_window_tokens=1000, model_settings=None),
        window_tokens=1000,
        sink=sink,
        run_id="test",
    )
    result = Agent(TestModel(call_tools=[], custom_output_text="compact summary")).run_sync(
        "continue",
        message_history=history,
        capabilities=[capability],
    )
    events = sink.events["test"]
    assert [event.data.phase for event in events][:3] == ["usage", "compacting", "compacted"]
    assert events[1].data.compaction_id == events[2].data.compaction_id
    assert events[2].data.used_tokens < events[1].data.used_tokens
    assert len(result.all_messages()) < len(history)
    assert events[-1].data.estimated is False
    assert events[-1].data.used_tokens == result.response.usage.input_tokens + result.response.usage.output_tokens
    for event in events:
        assert RUN_EVENT_ADAPTER.validate_json(event.model_dump_json()) == event


def test_unknown_window_is_not_reported_as_guessed_capacity():
    sink = InMemoryRunEventSink()
    capability = WorkbenchContextStatus(compaction=None, window_tokens=None, sink=sink, run_id="unknown")
    Agent(TestModel(call_tools=[])).run_sync("hello", capabilities=[capability])
    assert all(event.data.window_tokens is None for event in sink.events["unknown"])
    assert all(event.data.phase == "usage" for event in sink.events["unknown"])


def test_short_history_does_not_claim_compaction():
    sink = InMemoryRunEventSink()
    capability = WorkbenchContextStatus(
        compaction=build_compaction_capability(context_window_tokens=10000, model_settings=None),
        window_tokens=10000,
        sink=sink,
        run_id="small",
    )
    Agent(TestModel(call_tools=[])).run_sync("hello", capabilities=[capability])
    assert [event.data.phase for event in sink.events["small"]] == ["usage", "usage"]


def test_unknown_workbench_window_compacts_without_reporting_a_guessed_capacity():
    sink = InMemoryRunEventSink()
    capability = WorkbenchContextStatus(
        compaction=build_compaction_capability(context_window_tokens=None, model_settings=None, workbench=True),
        window_tokens=None,
        sink=sink,
        run_id="unknown-long",
    )
    history = [ModelRequest(parts=[UserPromptPart("Finish the original goal")])]
    for index in range(15):
        history.extend(
            [
                ModelResponse(parts=[TextPart(str(index) + "x" * 4_000)]),
                ModelRequest(parts=[UserPromptPart("continue")]),
            ]
        )
    result = Agent(TestModel(call_tools=[], custom_output_text="progress summary")).run_sync(
        "continue",
        message_history=history,
        capabilities=[capability],
    )
    events = sink.events["unknown-long"]
    assert all(event.data.window_tokens is None for event in events)
    assert [event.data.phase for event in events][:3] == ["usage", "compacting", "compacted"]
    assert len(result.all_messages()) < len(history)
