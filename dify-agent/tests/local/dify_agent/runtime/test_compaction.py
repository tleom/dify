import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness.compaction import (
    ClampOversizedMessages,
    ClearToolResults,
    SummarizingCompaction,
    TieredCompaction,
)

from dify_agent.runtime.compaction import build_compaction_capability


def test_workbench_summary_reads_old_tool_evidence_beyond_the_first_500_characters() -> None:
    from pydantic_ai.models.function import FunctionModel

    evidence = "VERIFIED_EXPORT=/workspace/reports/final-946.csv; rows=946; approval=review_only"
    history = [ModelRequest(parts=[UserPromptPart("Verify the export, preserve exact evidence and authorization")])]
    for index in range(8):
        history.extend(
            [
                ModelResponse(parts=[ToolCallPart("read", {"path": f"report-{index}"}, "reused-id")]),
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            "read", "prefix " * 500 + (evidence if index == 0 else "data") + " tail" * 700, "reused-id"
                        )
                    ]
                ),
            ]
        )
    observed = []

    def respond(messages, _info):
        text = "\n".join(str(getattr(part, "content", "")) for msg in messages for part in msg.parts)
        observed.append(text)
        return ModelResponse(parts=[TextPart(evidence if evidence in text else "Evidence absent")])

    capability = build_compaction_capability(context_window_tokens=8_000, model_settings=None, workbench=True)
    result = Agent(FunctionModel(respond)).run_sync("Continue", message_history=history, capabilities=[capability])
    assert any(evidence in text for text in observed), "The summarizer must see the complete old tool evidence"
    assert evidence in result.output
    assert history[1].parts[0].tool_call_id == "reused-id"


@pytest.mark.parametrize("workbench", [False, True])
def test_oversized_completed_tool_arguments_do_not_poison_the_next_request(workbench: bool) -> None:
    from pydantic_ai_harness.compaction import estimate_token_count

    history = [
        ModelRequest(parts=[UserPromptPart("Finish the report without repeating completed writes")]),
        ModelResponse(parts=[ToolCallPart("write", {"script": "x" * 100_000}, "done")]),
        ModelRequest(parts=[ToolReturnPart("write", {"saved": True}, "done")]),
    ]
    capability = build_compaction_capability(context_window_tokens=10_000, model_settings=None, workbench=workbench)
    result = Agent(TestModel(call_tools=[], custom_output_text="write saved=True; do not repeat it")).run_sync(
        "continue", message_history=history, capabilities=[capability]
    )
    assert estimate_token_count(result.all_messages()) < 8_000
    assert history[1].parts[0].args == {"script": "x" * 100_000}
    if workbench:
        assert any(
            isinstance(part, SystemPromptPart) and "saved=True" in part.content
            for message in result.all_messages()
            for part in message.parts
        )
    else:
        assert any(
            isinstance(part, ToolReturnPart) and part.content == {"saved": True}
            for message in result.all_messages()
            for part in message.parts
        )


def test_fewer_than_twenty_large_messages_still_compact_to_a_token_budget() -> None:
    from pydantic_ai_harness.compaction import estimate_token_count

    history = [ModelRequest(parts=[UserPromptPart("Finish the original task")])]
    for index in range(6):
        history.extend(
            [
                ModelResponse(parts=[TextPart(str(index) + "a" * 4_000)]),
                ModelRequest(parts=[UserPromptPart("continue")]),
            ]
        )
    capability = build_compaction_capability(context_window_tokens=3_000, model_settings=None)
    result = Agent(TestModel(call_tools=[], custom_output_text="progress summary")).run_sync(
        "continue", message_history=history, capabilities=[capability]
    )
    assert estimate_token_count(result.all_messages()) < 2_400


def test_build_compaction_capability_uses_effective_input_budget_and_standard_tiers() -> None:
    capability = build_compaction_capability(
        context_window_tokens=10_000,
        model_settings={"max_tokens": 3_000},
    )

    assert isinstance(capability, TieredCompaction)
    assert capability.target_tokens == 7_000
    assert len(capability.tiers) == 3
    assert isinstance(capability.tiers[0], ClampOversizedMessages)
    assert isinstance(capability.tiers[1], ClearToolResults)
    assert capability.tiers[1].keep_pairs == 3
    assert capability.tiers[1].clear_tool_inputs is False
    assert isinstance(capability.tiers[2], SummarizingCompaction)
    assert capability.tiers[2].model is None
    assert capability.tiers[2].keep_tokens == 3_500
    assert capability.tiers[2].preserve_first_user_message is True
    assert capability.tiers[2].incremental is True


def test_build_compaction_capability_uses_default_budget_and_handles_unknown_window() -> None:
    capability = build_compaction_capability(context_window_tokens=10_001, model_settings=None)

    assert isinstance(capability, TieredCompaction)
    assert capability.target_tokens == 8_000
    assert build_compaction_capability(context_window_tokens=None, model_settings=None) is None


@pytest.mark.parametrize(
    "max_tokens",
    [
        pytest.param(1_000, id="default-budget-wins"),
        pytest.param(0, id="zero-is-ignored"),
        pytest.param(-1, id="negative-is-ignored"),
    ],
)
def test_build_compaction_capability_uses_default_budget_when_output_reservation_is_smaller(
    max_tokens: int,
) -> None:
    capability = build_compaction_capability(
        context_window_tokens=10_000,
        model_settings={"max_tokens": max_tokens},
    )

    assert isinstance(capability, TieredCompaction)
    assert capability.target_tokens == 8_000


def test_build_compaction_capability_rejects_output_budget_that_consumes_window() -> None:
    with pytest.raises(ValueError, match="Model max_tokens must leave a positive input context budget"):
        _ = build_compaction_capability(
            context_window_tokens=1_000,
            model_settings={"max_tokens": 1_000},
        )


def test_compaction_clears_only_tool_results_older_than_the_last_three_pairs() -> None:
    history: list[ModelRequest | ModelResponse] = []
    for index in range(4):
        tool_call_id = f"call-{index}"
        history.extend(
            [
                ModelResponse(parts=[ToolCallPart("lookup", {"query": index}, tool_call_id)]),
                ModelRequest(parts=[ToolReturnPart("lookup", "x" * 4_000, tool_call_id)]),
            ]
        )

    capability = build_compaction_capability(context_window_tokens=4_100, model_settings=None)
    assert capability is not None
    agent = Agent[None, str](TestModel(call_tools=[]), deps_type=type(None))
    result = agent.run_sync("next", message_history=history, capabilities=[capability])

    tool_returns = [
        part
        for message in result.all_messages()
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert [part.content for part in tool_returns] == ["[tool result cleared]", *("x" * 4_000 for _ in range(3))]


def test_compaction_summary_is_present_in_full_run_history() -> None:
    history: list[ModelRequest | ModelResponse] = []
    for index in range(30):
        history.extend(
            [
                ModelRequest(parts=[UserPromptPart(f"user-{index}-" + "u" * 120)]),
                ModelResponse(parts=[TextPart(f"assistant-{index}-" + "a" * 120)], model_name="test"),
            ]
        )

    capability = build_compaction_capability(context_window_tokens=1_000, model_settings=None)
    assert capability is not None
    agent = Agent[None, str](
        TestModel(call_tools=[], custom_output_text="summary body"),
        deps_type=type(None),
    )
    result = agent.run_sync(
        "next",
        message_history=history,
        capabilities=[capability],
    )

    messages = result.all_messages()
    assert len(messages) < len(history)
    assert isinstance(messages[0], ModelRequest)
    assert len(messages[0].parts) == 1
    assert isinstance(messages[0].parts[0], SystemPromptPart)
    assert messages[0].parts[0].content == "Summary of previous conversation:\n\nsummary body"
    assert any(
        isinstance(part, UserPromptPart) and str(part.content).startswith("user-0-")
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )
