import json
from types import SimpleNamespace as NS

import pytest
from werkzeug.exceptions import NotFound, Conflict

from services.workbench import branches


def run(id, **payload):
    return NS(id=id, payload=json.dumps(payload), status='completed', chat_id='chat',
              tenant_id='tenant', account_id='owner')


def test_regeneration_is_sibling_and_children_keep_explicit_parent():
    runs = [run('a'), run('b', branch_parent_run_id='a'), run('b2', branch_parent_run_id='a'),
            run('a2', regenerate_from='a'), run('c', branch_parent_run_id='b')]
    assert branches.parent_links(runs) == {'a': None, 'b': 'a', 'b2': 'a', 'a2': None, 'c': 'b'}


def test_selected_native_parent_is_scoped_and_regeneration_reuses_original_parent(monkeypatch):
    from services.workbench import message_actions
    from services.message_service import MessageService
    runs = [run('a'), run('b', branch_parent_run_id='a'), run('a2', regenerate_from='a')]
    monkeypatch.setattr(branches, 'chat_runs', lambda *args: runs)
    monkeypatch.setattr(message_actions, 'message_ids', lambda item: ['m-' + item.id])
    chat = NS(app_id='app', account_id='owner', tenant_id='tenant', conversation_id='conversation')
    session = NS(get=lambda *args: NS(tenant_id='tenant'))
    message = NS(conversation_id='conversation')
    monkeypatch.setattr(MessageService, 'get_message', lambda **kwargs: message)
    assert branches.resolve_parent(session, chat, {'parent_message_id': 'm-b'}) == {
        'branch_parent_run_id': 'b', 'parent_message_id': 'm-b'}
    assert branches.resolve_parent(session, chat, {'regenerate_from': 'b'}) == {
        'branch_parent_run_id': 'a', 'parent_message_id': 'm-a'}
    assert branches.resolve_parent(session, chat, {'parent_message_id': None})['branch_parent_run_id'] is None
    message.conversation_id = 'other'
    with pytest.raises(NotFound):
        branches.resolve_parent(session, chat, {'parent_message_id': 'm-b'})
    with pytest.raises(NotFound):
        branches.resolve_parent(session, chat, {'regenerate_from': 'unknown'})
    runs[-1].status = 'running'
    with pytest.raises(Conflict):
        branches.resolve_parent(session, chat, {})


def test_selected_history_keeps_exact_multimodal_and_tool_records():
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart, ToolCallPart, ToolReturnPart
    from agenton_collections.layers.pydantic_ai import PydanticAIHistoryRuntimeState
    state = PydanticAIHistoryRuntimeState(messages=[
        ModelRequest(parts=[UserPromptPart('old branch')]),
        ModelResponse(parts=[ToolCallPart('read_file', {'path': 'x'}, tool_call_id='call')]),
        ModelRequest(parts=[ToolReturnPart('read_file', 'old file contents', tool_call_id='call')]),
        ModelResponse(parts=[TextPart('old branch answer')]),
    ]).model_dump(mode='json')
    parent = run('a', output_history=state)
    assert branches.output_history(None, parent) == state
    assert json.loads(parent.payload)['output_history'] == state


def test_failed_before_snapshot_keeps_input_history():
    parent = run('a', input_history={'messages': []})
    parent.status = 'failed'
    assert branches.output_history(None, parent) == {'messages': []}
    parent.status = 'completed'
    with pytest.raises(Conflict):
        branches.output_history(None, parent)
