"""Manual context summary using the installed harness's native compaction API."""

import copy
import hashlib

from pydantic_ai.messages import ModelMessage, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.compaction import (
    SummarizingCompaction,
    compact_now,
    estimate_context_tokens,
    estimate_token_count,
)

from dify_agent.protocol.schemas import ContextStatusData, ContextStatusRunEvent
from dify_agent.runtime.history import replace_run_history


def _unique_tool_pairs(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Give repeated provider call IDs distinct names on the compaction copy.

    Some providers restart numbering at every model response. The harness checks
    IDs across the entire history, so an old call otherwise appears paired with
    every later return having the same ID and prevents a safe cut.
    """
    result = copy.deepcopy(messages)
    seen: set[str] = set()
    pairs: dict[tuple[str, str | None], str] = {}
    for index, message in enumerate(result):
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                original = part.tool_call_id
                identifier = original
                if identifier in seen:
                    identifier = (
                        "call_" + hashlib.sha256(f"{index}:{original}:{part.tool_name}".encode()).hexdigest()[:32]
                    )
                seen.add(identifier)
                pairs[(original, part.tool_name)] = identifier
                part.tool_call_id = identifier
            elif isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_call_id:
                part.tool_call_id = pairs.get((part.tool_call_id, part.tool_name), part.tool_call_id)
    return result


async def compact_history(*, layer, model, history, checkpoint, sink, run_id, window_tokens):
    command = layer.runtime_state.control or {}
    if command.get("kind") != "compact" or history is None:
        raise ValueError("Manual compaction requires an owned command and existing history")
    identifier = command["id"]
    original = copy.deepcopy(list(history.message_history))
    before = estimate_context_tokens(original)
    usage = RunUsage()

    async def publish(phase, *, after=None, message=None):
        await layer.request(
            "compact_result",
            {
                "id": identifier,
                "phase": phase,
                "before_tokens": before,
                "after_tokens": after,
                "message": message,
            },
            request_key=f"compact:{identifier}:{phase}",
        )
        await sink.append_event(
            ContextStatusRunEvent(
                run_id=run_id,
                data=ContextStatusData(
                    phase="compacted" if phase == "unchanged" else phase,
                    used_tokens=after if after is not None else before,
                    before_tokens=before,
                    window_tokens=window_tokens,
                    compaction_id=identifier,
                ),
            )
        )

    await publish("compacting")
    try:
        # Directly run the summarizing tier: the normal automatic trigger would
        # deliberately do nothing below the context-pressure threshold.
        result = await compact_now(
            SummarizingCompaction(
                max_tokens=1,
                keep_messages=4,
                preserve_first_user_message=True,
                incremental=True,
            ),
            _unique_tool_pairs(original),
            model=model,
            focus=command.get("focus") or None,
            usage=usage,
        )
        # The preserved last response still reports usage for the OLD request.
        # Measure reclaimed text, as the native tiered strategy does, instead
        # of treating that stale provider usage as the size of the new history.
        reclaimed = estimate_token_count(original) - estimate_token_count(result)
        after = max(0, before - reclaimed)
        changed = reclaimed > 0
        if changed:
            # A visible success must have a recoverable native checkpoint first.
            if checkpoint is None:
                raise RuntimeError("Manual compaction requires durable history checkpointing")
            await checkpoint.save(result)
            replace_run_history(history, result)
        message = f"上下文已压缩，估算用量从 {before} 降至 {after}" if changed else "当前上下文无需进一步压缩"
        await publish("compacted" if changed else "unchanged", after=after if changed else before, message=message)
        return message, usage
    except Exception:
        await publish("failed", message="上下文压缩未完成，原始记录已保留")
        raise
