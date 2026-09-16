"""Manual context summary using the installed harness's native compaction API."""

import asyncio
import copy

from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.compaction import (
    compact_now,
    estimate_context_tokens,
    estimate_token_count,
)

from dify_agent.protocol.schemas import ContextStatusData, ContextStatusRunEvent
from dify_agent.runtime.history import replace_run_history
from dify_agent.runtime.workbench_compaction import WorkbenchSummarizingCompaction


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
            WorkbenchSummarizingCompaction(
                max_tokens=1,
                keep_messages=4,
                preserve_first_user_message=True,
                incremental=True,
            ),
            original,
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
    except Exception:
        await publish("failed", message="上下文压缩未完成，原始记录已保留")
        raise

    if changed:
        replace_run_history(history, result)
    # History is already committed. A failed notification must never change the
    # outcome to "failed / original retained". The stable key makes retries safe
    # when the API committed its state but the event transport lost its response.
    message = f"上下文已压缩，估算用量从 {before} 降至 {after}" if changed else "当前上下文无需进一步压缩"
    for attempt in range(3):
        try:
            await publish("compacted" if changed else "unchanged", after=after if changed else before, message=message)
            break
        except Exception as error:
            if attempt == 2:
                raise RuntimeError(message + "；完成状态同步失败，请刷新后核对") from error
            await asyncio.sleep(0.2 * (attempt + 1))
    return message, usage
