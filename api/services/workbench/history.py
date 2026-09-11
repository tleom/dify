"""Bridge a stopped workbench turn to the runtime's native interrupted-history handling."""

from dataclasses import replace
from datetime import UTC

from agenton.compositor import CompositorSessionSnapshot
from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, ToolReturnPart, UserPromptPart


def mark_interrupted_history(snapshot: CompositorSessionSnapshot | None) -> CompositorSessionSnapshot | None:
    """Keep the captured call and let Pydantic AI close it without executing it again."""
    if snapshot is None:
        return None
    layers = list(snapshot.layers)
    for index, layer in enumerate(layers):
        if layer.name != "history":
            continue
        state = PydanticAIHistoryRuntimeState.model_validate(layer.runtime_state)
        if not state.messages:
            return snapshot
        last = state.messages[-1]
        if last.state != "complete":
            return snapshot
        response_index = next(
            (i for i in range(len(state.messages) - 1, -1, -1) if isinstance(state.messages[i], ModelResponse)),
            None,
        )
        if response_index is None:
            return snapshot
        response = state.messages[response_index]
        assert isinstance(response, ModelResponse)
        pending = {call.tool_call_id for call in response.tool_calls}
        for message in state.messages[response_index + 1 :]:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, (ToolReturnPart, RetryPromptPart)):
                        pending.discard(part.tool_call_id)
        if not pending:
            return snapshot
        state.messages = [*state.messages[:-1], replace(last, state="interrupted")]
        layers[index] = layer.model_copy(update={"runtime_state": state.model_dump(mode="json")})
        return snapshot.model_copy(update={"layers": layers})
    return snapshot


def history_state(snapshot):
    if snapshot is not None:
        for layer in snapshot.layers:
            if layer.name == "history":
                return layer.runtime_state
    return None


def restore_history(snapshot, state):
    if snapshot is None:
        return None
    # Validate the same runtime schema used by Dify's Agent backend.
    parsed = (
        PydanticAIHistoryRuntimeState.model_validate(state)
        if state is not None
        else PydanticAIHistoryRuntimeState(messages=[])
    )
    return snapshot.model_copy(
        update={
            "layers": [
                layer.model_copy(update={"runtime_state": parsed.model_dump(mode="json")})
                if layer.name == "history"
                else layer
                for layer in snapshot.layers
            ]
        }
    )


def history_before_message(snapshot, query, created_at):
    """Recover the input history of an older Dify message without deleting its stored answer."""
    state = history_state(snapshot)
    if state is None:
        return snapshot
    history = PydanticAIHistoryRuntimeState.model_validate(state)
    cutoff = created_at.replace(tzinfo=UTC) if created_at.tzinfo is None else created_at
    for index, message in enumerate(history.messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart) or part.timestamp < cutoff:
                continue
            content = part.content
            matches = content == query or (isinstance(content, list) and query in content)
            if matches:
                history.messages = history.messages[:index]
                return restore_history(snapshot, history.model_dump(mode="json"))
    raise ValueError("原消息的上下文已不可用，请重新发送该消息")
