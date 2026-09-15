import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from dify_agent.runtime_backend import workbench


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
