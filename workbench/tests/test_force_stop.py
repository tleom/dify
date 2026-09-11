from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid5


def test_force_stop_fences_the_deterministic_ticket_and_releases_only_after_confirmation(monkeypatch):
    from tasks import workbench_tasks as tasks
    run = SimpleNamespace(id='run', account_id='owner', tenant_id='tenant', backend_run_id=None, payload='{}')
    @contextmanager
    def session():
        yield SimpleNamespace(get=lambda *args: run)
    monkeypatch.setattr(tasks.session_factory, 'create_session', session)
    stop, fence, release, event, reconcile = Mock(), Mock(return_value=False), Mock(), Mock(), Mock()
    monkeypatch.setattr(tasks, 'stop_native', stop)
    monkeypatch.setattr(tasks, 'fence_remote', fence)
    monkeypatch.setattr(tasks.scheduler, 'release', release)
    monkeypatch.setattr(tasks, 'event', event)
    monkeypatch.setattr(tasks.reconcile, 'delay', reconcile)
    tasks.force_stop.run('run', 'owner')
    fence.assert_called_once_with(str(uuid5(NAMESPACE_URL, 'dify-workbench-run:run:0')))
    release.assert_not_called()
    event.assert_called_once_with('run', {'event':'workbench_end','status':'cancelled','error':None})
    fence.return_value = True
    tasks.force_stop.run('run', 'owner')
    release.assert_called_once_with('tenant:owner', 'run')
