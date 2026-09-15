"""Exercise the real installed harness and checkpoint-before-success contract."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from dify_agent.runtime.manual_compaction import compact_history


@pytest.mark.anyio
@pytest.mark.parametrize("checkpoint_failure", [False, True])
@pytest.mark.parametrize("repeated_call_ids", [False, True])
@pytest.mark.parametrize("provider_usage", [False, True])
async def test_manual_summary_preserves_original_task_and_paired_tail(
    checkpoint_failure, repeated_call_ids, provider_usage
):
    messages = [ModelRequest(parts=[UserPromptPart("原始目标：完整核验报告")])]
    for number in range(12):
        messages.extend(
            [
                ModelResponse(parts=[TextPart(f"调查步骤 {number}: " + "已验证的材料。" * 1000)]),
                ModelRequest(parts=[UserPromptPart("继续核验")]),
            ]
        )
        if repeated_call_ids:
            messages.extend(
                [
                    ModelResponse(parts=[ToolCallPart("read_file", {"path": "report.md"}, "read-0")]),
                    ModelRequest(parts=[ToolReturnPart("read_file", "材料。" * 1000, "read-0")]),
                ]
            )
    messages.extend(
        [
            ModelResponse(
                parts=[ToolCallPart("read_file", {"path": "report.md"}, "last-read")],
                usage=RequestUsage(input_tokens=100000, output_tokens=100) if provider_usage else RequestUsage(),
            ),
            ModelRequest(parts=[ToolReturnPart("read_file", "已取得报告", "last-read")]),
        ]
    )
    original = copy.deepcopy(messages)
    history = SimpleNamespace(message_history=messages)
    history.replace_messages = lambda value: setattr(history, "message_history", value)
    phases = []

    async def publish(_action, data, **_kwargs):
        phases.append(data["phase"])

    layer = SimpleNamespace(
        runtime_state=SimpleNamespace(control={"kind": "compact", "id": "summary"}), request=publish
    )
    checkpoint = SimpleNamespace(
        save=AsyncMock(side_effect=OSError("checkpoint unavailable") if checkpoint_failure else None)
    )
    params = dict(
        layer=layer,
        model=TestModel(call_tools=[], custom_output_text="已核验材料；保留原目标并继续最终检查。"),
        history=history,
        checkpoint=checkpoint,
        sink=SimpleNamespace(append_event=AsyncMock()),
        run_id="run",
        window_tokens=128000,
    )
    if checkpoint_failure:
        with pytest.raises(OSError, match="checkpoint unavailable"):
            await compact_history(**params)
        assert history.message_history == original
        assert phases == ["compacting", "failed"]
    else:
        message, _usage = await compact_history(**params)
        assert "已压缩" in message
        assert len(history.message_history) < len(original)
        assert any(message == original[0] for message in history.message_history)
        assert history.message_history[-2:] == original[-2:]
        assert phases == ["compacting", "compacted"]
        checkpoint.save.assert_awaited_once_with(history.message_history)
    assert messages == original
