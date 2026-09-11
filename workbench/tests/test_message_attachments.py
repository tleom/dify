"""Existing stored tasks must expose their actual attachments without rewriting history."""
import json
from types import SimpleNamespace

import pytest

from services.workbench.service import run_dto


@pytest.mark.parametrize('paths,expected', [
    (None, []),
    (['/workspace/shared/地址解析结果_2026.csv', '/workspace/conversations/chat/资料 (2).txt'], [
        {'path': 'shared/地址解析结果_2026.csv', 'name': '地址解析结果_2026.csv'},
        {'path': 'conversations/chat/资料 (2).txt', 'name': '资料 (2).txt'},
    ]),
    (['/outside/private.txt', None, '/workspace/shared/'], []),
])
def test_stored_attachments(paths, expected):
    payload = {'query': '这是啥'}
    if paths is not None:
        payload['sandbox_paths'] = paths
    run = SimpleNamespace(id='run', chat_id='chat', revision_id='rev', status='completed',
                          error=None, event_log='[]', payload=json.dumps(payload))
    before = run.payload
    result = run_dto(run)
    assert result['attachments'] == expected
    assert result['query'] == '这是啥'
    assert run.payload == before


def test_image_only_message_and_empty_message_validation():
    from pydantic import ValidationError

    from controllers.console.workbench import WorkbenchRunPayload

    payload = {'version': 1, 'request_key': 'image-only', 'query': ''}
    for query in ['', '  ']:
        with pytest.raises(ValidationError):
            WorkbenchRunPayload.model_validate({**payload, 'query': query})
    message = WorkbenchRunPayload.model_validate({
        **payload, 'files': [{'path': 'shared/image.png', 'version': 'a' * 64}],
    })
    assert message.query == ''
    assert message.files[0].path == 'shared/image.png'
