from contextlib import contextmanager
from types import SimpleNamespace
import json

from flask import Flask
import pytest


@pytest.mark.parametrize('lease,expected', [(123.0,'stopping'), (None,'cancelled')])
def test_cancelled_response_waits_for_remote_lease_release(monkeypatch,lease,expected):
    from extensions import ext_redis
    from services.workbench.message_actions import with_feedback

    @contextmanager
    def pipeline(**kwargs):
        yield SimpleNamespace(zscore=lambda *args:None,execute=lambda:[lease])
    monkeypatch.setattr(ext_redis,'redis_client',SimpleNamespace(pipeline=pipeline))
    dto={'id':'run','status':'cancelled','message_id':None,'error':None}
    actual=with_feedback(None,[SimpleNamespace(account_id='owner')],[dto])[0]
    assert actual['status']==expected


def test_sse_does_not_end_before_remote_cancel_is_confirmed(monkeypatch):
    from controllers.console import workbench as controller
    reads=0
    def read(*args,**kwargs):
        nonlocal reads
        reads+=1
        if reads==1:
            return [(b'stream',[(b'1-0',{b'data':json.dumps({'event':'workbench_end','status':'cancelled'}).encode()})])]
        return []
    def owned(*args):
        return {'status':'cancelled' if reads>1 else 'stopping','error':None,'events':[]},None
    monkeypatch.setattr(controller,'redis_client',SimpleNamespace(xread=read))
    monkeypatch.setattr(controller,'owned_run',owned)
    monkeypatch.setattr(controller.Events,'owner',staticmethod(lambda:('tenant','owner')))
    with Flask('test').test_request_context('/'):
        response=controller.Events().get('run')
        body=response.get_data(as_text=True)
    events=[json.loads(line[5:]) for line in body.splitlines() if line.startswith('data:')]
    assert events==[{'event':'workbench_status','status':'stopping'},{'event':'workbench_end','status':'cancelled','error':None}]
    assert reads==2
