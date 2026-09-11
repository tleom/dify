from contextlib import contextmanager
import json
from types import SimpleNamespace

from agenton.compositor import CompositorSessionSnapshot, LayerSessionSnapshot
from agenton.layers import LifecycleState
from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState
from pydantic_ai import Agent
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
import pytest

from services.workbench.history import mark_interrupted_history


def test_regeneration_restores_original_input_without_previous_answer():
    from services.workbench.history import history_state, restore_history

    original = snapshot([ModelRequest(parts=[UserPromptPart('earlier task')]), ModelResponse(parts=[TextPart('earlier result')])])
    finished = snapshot([
        *PydanticAIHistoryRuntimeState.model_validate(original.layers[0].runtime_state).messages,
        ModelRequest(parts=[UserPromptPart('regenerate this')]), ModelResponse(parts=[TextPart('old answer to replace')]),
    ])
    restored = restore_history(finished, history_state(original))
    history = PydanticAIHistoryRuntimeState.model_validate(restored.layers[0].runtime_state).messages
    seen = []
    def model(messages, info):
        seen.extend(messages)
        return ModelResponse(parts=[TextPart('regenerated answer')])
    assert Agent(FunctionModel(model)).run_sync('regenerate this', message_history=history).output == 'regenerated answer'
    serialized = str(seen)
    assert 'earlier result' in serialized and 'old answer to replace' not in serialized
    assert restored.layers[1] == finished.layers[1]
    assert 'old answer to replace' in finished.model_dump_json()
    assert not PydanticAIHistoryRuntimeState.model_validate(restore_history(finished, None).layers[0].runtime_state).messages


def test_older_message_regeneration_uses_timestamp_to_distinguish_repeated_prompts():
    from datetime import UTC, datetime, timedelta
    from services.workbench.history import history_before_message

    now = datetime.now(UTC)
    original = snapshot([
        ModelRequest(parts=[UserPromptPart('same query', timestamp=now-timedelta(minutes=2))]),
        ModelResponse(parts=[TextPart('earlier same question answer')]),
        ModelRequest(parts=[UserPromptPart('same query', timestamp=now+timedelta(seconds=1))]),
        ModelResponse(parts=[TextPart('target answer')]),
    ])
    restored = history_before_message(original, 'same query', now.replace(tzinfo=None))
    assert 'earlier same question answer' in restored.model_dump_json()
    assert 'target answer' not in restored.model_dump_json()
    with pytest.raises(ValueError, match='上下文已不可用'):
        history_before_message(original, 'missing query', now)


def snapshot(messages):
    return CompositorSessionSnapshot(layers=[
        LayerSessionSnapshot(name='history', lifecycle_state=LifecycleState.SUSPENDED,
            runtime_state=PydanticAIHistoryRuntimeState(messages=messages).model_dump(mode='json')),
        LayerSessionSnapshot(name='runtime', lifecycle_state=LifecycleState.SUSPENDED, runtime_state={'keep': 'binding'}),
    ])


@pytest.mark.parametrize('partial_results', [False, True])
def test_native_runtime_accepts_new_prompt_without_reexecuting_interrupted_tool(partial_results):
    messages = [
        ModelRequest(parts=[UserPromptPart('previous completed task')]),
        ModelResponse(parts=[ToolCallPart('shell_run', {'command':'earlier'}, tool_call_id='shell_run')]),
        ModelRequest(parts=[ToolReturnPart('shell_run','earlier result',tool_call_id='shell_run')]),
        ModelResponse(parts=[TextPart('earlier answer')]),
        ModelRequest(parts=[UserPromptPart('stopped task')]),
        ModelResponse(parts=[ToolCallPart('shell_run', '{"command":', tool_call_id='shell_run')]),
    ]
    if partial_results:
        messages[-1].parts.append(ToolCallPart('shell_run', {'command':'already finished'}, tool_call_id='finished'))
        messages.append(ModelRequest(parts=[ToolReturnPart('shell_run','real output',tool_call_id='finished')]))
    original = snapshot(messages)
    original_json = original.model_dump_json()
    seen = []

    def model(received, info):
        seen.extend(received)
        return ModelResponse(parts=[TextPart('FOLLOWUP_OK')])

    agent = Agent(FunctionModel(model))

    @agent.tool_plain
    def shell_run(command: str) -> str:
        raise AssertionError('interrupted tool must never execute again')

    if not partial_results:
        with pytest.raises(UserError, match='unprocessed tool calls'):
            agent.run_sync('next user message', message_history=messages)
    repaired = mark_interrupted_history(original)
    history = PydanticAIHistoryRuntimeState.model_validate(repaired.layers[0].runtime_state).messages
    result = agent.run_sync('next user message', message_history=history)
    assert result.output == 'FOLLOWUP_OK'
    returns = [part for message in seen if isinstance(message, ModelRequest) for part in message.parts if isinstance(part, ToolReturnPart)]
    interrupted = [part for part in returns if part.outcome == 'interrupted']
    assert len(interrupted) == 1
    assert interrupted[0].tool_call_id == 'shell_run'
    assert any(part.content == 'earlier result' for part in returns)
    if partial_results:
        assert any(part.content == 'real output' for part in returns)
    assert original.model_dump_json() == original_json
    assert repaired.layers[1] == original.layers[1]
    assert mark_interrupted_history(repaired) is repaired


def test_completed_history_is_unchanged():
    original = snapshot([
        ModelResponse(parts=[ToolCallPart('shell_run',{},tool_call_id='done')]),
        ModelRequest(parts=[ToolReturnPart('shell_run','done',tool_call_id='done')]),
    ])
    assert mark_interrupted_history(original) is original
    assert mark_interrupted_history(None) is None


@pytest.mark.parametrize('previous_status,continuation,changed', [
    ('cancelled', None, True),
    ('failed', None, True),
    ('interrupted', None, True),
    ('waiting_input', None, False),
    ('completed', None, False),
    ('cancelled', {'tool_results': {}}, False),
])
def test_recovery_is_scoped_to_new_workbench_turn_after_terminal_interruption(monkeypatch, previous_status, continuation, changed):
    from services.workbench import runtime, scheduler
    run = SimpleNamespace(id='new-run',chat_id='same-chat',payload=json.dumps({'continuation':continuation}))
    previous = SimpleNamespace(status=previous_status)

    @contextmanager
    def session():
        yield SimpleNamespace(scalar=lambda query:previous)

    monkeypatch.setattr(runtime.dify_config,'WORKBENCH_ENABLED',True)
    monkeypatch.setattr(runtime.session_factory,'get_session_maker',lambda:SimpleNamespace(begin=session))
    monkeypatch.setattr(runtime,'current_run',lambda *args:run)
    monkeypatch.setattr(scheduler,'heartbeat',lambda *args:True)
    original = snapshot([ModelResponse(parts=[ToolCallPart('shell_run',{},tool_call_id='pending')])])
    request = SimpleNamespace(session_snapshot=original)
    runtime.prepare_execution('tenant','conversation','account',request)
    assert (request.session_snapshot is not original) is changed
    assert request.execution_ticket == run.backend_run_id
