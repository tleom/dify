import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from dify_agent.runtime_backend import workbench
from dify_agent.runtime_backend.errors import BindingLostError
from dify_agent.runtime_backend.protocols import RuntimeLayout


def test_keepalive_survives_a_manager_outage_and_remains_cancellable(monkeypatch):
    backend = workbench.WorkbenchExecutionBindingBackend(
        fallback=None, manager_endpoint="http://manager", manager_token="test"
    )
    manager = AsyncMock(side_effect=[httpx.ConnectError("temporarily unavailable"), {"ok": True}])
    monkeypatch.setattr(backend, "_manager", manager)
    waits = 0

    async def sleep(_seconds):
        nonlocal waits
        waits += 1
        if waits == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(workbench.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backend._keep_alive("workspace"))
    assert manager.await_count == 2
    manager.assert_awaited_with("workspace", "touch")


def test_release_stops_conversation_jobs_and_closes_transport():
    async def check():
        backend = workbench.WorkbenchExecutionBindingBackend(
            fallback=None, manager_endpoint="http://manager", manager_token="test"
        )
        backend._manager = AsyncMock(return_value={"stopped": 1})
        transport = SimpleNamespace(close=AsyncMock())
        pulse = asyncio.create_task(asyncio.sleep(1000))
        lease = workbench.WorkbenchRuntimeLease(transport, pulse, "workspace", "binding")
        await backend.release(lease)
        backend._manager.assert_awaited_once_with("workspace", "stop-binding", {"binding_id": "binding"})
        transport.close.assert_awaited_once()
        assert pulse.cancelled()

    asyncio.run(check())


@pytest.mark.parametrize("missing_home", [False, True])
def test_acquire_uses_shared_root_to_prepare_real_conversation_and_preserves_home_loss(monkeypatch, missing_home):
    async def check():
        binding = "12345678-1234-5678-1234-567812345678"
        owner = "22345678-1234-5678-1234-567812345678"
        backend = workbench.WorkbenchExecutionBindingBackend(
            fallback=None, manager_endpoint="http://manager", manager_token="test"
        )
        raw = f"{binding}:{owner}"
        # The unused account directory is absent; querying it would fail before
        # the real conversation layout could be set, as the old acquire did.
        lease = SimpleNamespace(
            layout=RuntimeLayout(home_dir=f"/home/dify/{binding}", workspace_dir=f"/workspace/{owner}"),
            client=object(),
            close=AsyncMock(),
        )
        control = SimpleNamespace(commands=object(), close=AsyncMock())
        local = SimpleNamespace(
            _lease=lambda _ref: lease,
            _control_lease=lambda _ref: control,
            acquire=AsyncMock(side_effect=RuntimeError("invalid_cwd")),
        )
        backend._backend = AsyncMock(return_value=local)
        execute = AsyncMock(return_value=SimpleNamespace(exit_code=1 if missing_home else 0))
        monkeypatch.setattr(workbench, "run_shellctl_control_command", execute)
        if missing_home:
            with pytest.raises(BindingLostError):
                await backend.acquire("wb:" + raw)
            lease.close.assert_awaited_once()
        else:
            acquired = await backend.acquire("wb:" + raw)
            assert acquired.layout.workspace_dir == f"/workspace/conversations/{binding}"
            assert acquired.commands.default_cwd == acquired.layout.workspace_dir
            acquired.pulse.cancel()
            with pytest.raises(asyncio.CancelledError):
                await acquired.pulse
        local.acquire.assert_not_awaited()
        control.close.assert_awaited_once()
        script = execute.await_args.args[1]
        assert f"test -d /home/dify/{binding}" in script
        assert f"/workspace/{owner}" not in script
        assert f"mkdir -p /home/dify/{binding}/tmp /workspace/conversations/{binding}" in script

    asyncio.run(check())
