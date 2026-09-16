"""Real Redis and process-loss checks; run only against an isolated test Redis.

The restart test coordinates with an external fixture supervisor through a
private signal directory. Neither test restarts a service by itself.
"""

import asyncio
import importlib.util
import json
import multiprocessing
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from agenton.compositor import CompositorSessionSnapshot
from dify_agent.protocol import CancelRunRequest
from dify_agent.runtime.runner import AgentRunRunner
from dify_agent.storage.redis_run_store import RedisRunStore

pytestmark = pytest.mark.integration


def settings():
    url = os.environ.get("DIFY_AGENT_TEST_REDIS_URL")
    if not url:
        pytest.skip("DIFY_AGENT_TEST_REDIS_URL must select an isolated test Redis")
    return url, "workbench-fault-test:" + str(uuid4())


async def wait_until(predicate, *, seconds=30):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Fault fixture did not reach the expected state")
        await asyncio.sleep(0.05)


def native_child(url, prefix, run_id, directory):

    from tests.local.dify_agent.runtime.test_workbench_activity import _call, _setup

    root = Path(directory)
    requests = 0

    async def write_once():
        with (root / "completed.txt").open("a") as stream:
            stream.write("saved once\n")
        return {"saved": "completed.txt", "verified": True}

    async def slow():
        (root / "slow-started").write_text("waiting")
        await asyncio.Event().wait()

    async def model(messages, info):
        nonlocal requests
        requests += 1
        yield {0: _call("write_once" if requests == 1 else "slow", {}, f"call-{requests}")}

    async def run():
        redis = Redis.from_url(url)
        store = RedisRunStore(redis, prefix=prefix)
        await store.create_run_once(run_id)
        with pytest.MonkeyPatch.context() as patch:
            request, _, _ = _setup(patch, model, [Tool(write_once), Tool(slow)])
            async with httpx.AsyncClient() as client:
                await AgentRunRunner(
                    run_id=run_id,
                    request=request,
                    sink=store,
                    plugin_daemon_http_client=client,
                    dify_api_http_client=client,
                ).run()
        await redis.aclose()

    asyncio.run(run())


def test_native_process_death_recovers_completed_work_without_replaying_pending_call(tmp_path):
    from tests.local.dify_agent.runtime.test_workbench_activity import _setup

    url, prefix = settings()
    bridge_path = os.environ.get("WORKBENCH_API_HISTORY_SOURCE")
    if not bridge_path:
        pytest.skip("WORKBENCH_API_HISTORY_SOURCE must select the actual API history bridge source")
    spec = importlib.util.spec_from_file_location("workbench_history_bridge", bridge_path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    run_id = str(uuid4())
    child = multiprocessing.get_context("spawn").Process(
        target=native_child,
        args=(url, prefix, run_id, str(tmp_path)),
    )
    child.start()

    async def scenario():
        redis = Redis.from_url(url)
        store = RedisRunStore(redis, prefix=prefix)
        try:
            await wait_until(lambda: (tmp_path / "slow-started").exists() or not child.is_alive())
            assert child.is_alive(), f"Native child exited early: {child.exitcode}"
            child.kill()
            await asyncio.to_thread(child.join, 5)
            assert child.exitcode is not None
            assert (await store.get_run(run_id)).status == "running"
            assert await store.fence_run(run_id) == "running"
            intent = await store.get_cancellation_intent(run_id)
            assert intent is not None
            await store.finalize_cancellation(run_id, intent)
            history = await store.get_history_checkpoint(run_id)
            assert history is not None
            assert "completed.txt" in json.dumps(history)
            assert any(part.get("tool_name") == "slow" for part in history["messages"][-1]["parts"])
            _, created = await store.create_run_once(run_id)
            assert not created

            async def must_not_replay():
                raise AssertionError("An interrupted external operation was replayed")

            async def resumed_model(messages, info):
                parts = [part for message in messages for part in message.parts]
                assert any(isinstance(part, ToolCallPart) and part.tool_name == "slow" for part in parts)
                assert any(isinstance(part, ToolReturnPart) and part.tool_name == "slow" for part in parts)
                assert (tmp_path / "completed.txt").read_text() == "saved once\n"
                yield "continued from saved progress"

            with pytest.MonkeyPatch.context() as patch:
                request, _, _ = _setup(
                    patch,
                    resumed_model,
                    [Tool(must_not_replay, name="write_once"), Tool(must_not_replay, name="slow")],
                )
                snapshot = CompositorSessionSnapshot.model_validate(
                    {
                        "schema_version": 1,
                        "layers": [
                            {
                                "name": layer.name,
                                "lifecycle_state": "suspended",
                                "runtime_state": history if layer.name == "history" else {},
                            }
                            for layer in request.composition.layers
                        ],
                    }
                )
                request.session_snapshot = bridge.mark_interrupted_history(snapshot)
                next_id = str(uuid4())
                await store.create_run_once(next_id)
                async with httpx.AsyncClient() as client:
                    await AgentRunRunner(
                        run_id=next_id,
                        request=request,
                        sink=store,
                        plugin_daemon_http_client=client,
                        dify_api_http_client=client,
                    ).run()
                record = await store.get_run(next_id)
                assert record.status == "succeeded", record.error
                assert (tmp_path / "completed.txt").read_text() == "saved once\n"
        finally:
            if child.is_alive():
                child.kill()
                await asyncio.to_thread(child.join, 5)
            keys = [key async for key in redis.scan_iter(match=prefix + ":*")]
            if keys:
                await redis.delete(*keys)
            await redis.aclose()

    asyncio.run(scenario())


def test_real_redis_restart_preserves_ticket_and_resumes_checkpoint_and_cancellation():
    url, prefix = settings()
    signals_path = os.environ.get("WORKBENCH_FAULT_SIGNALS")
    if not signals_path:
        pytest.skip("WORKBENCH_FAULT_SIGNALS requires an external test Redis supervisor")
    signals = Path(signals_path)
    signals.mkdir(parents=True, exist_ok=True)

    async def scenario():
        redis = Redis.from_url(url, socket_connect_timeout=1, retry=Retry(NoBackoff(), 0))
        store = RedisRunStore(redis, prefix=prefix)
        run_id = str(uuid4())
        observer = None
        try:
            await store.create_run_once(run_id)
            await store.checkpoint_history(run_id, '{"messages":[]}')
            observer = asyncio.create_task(store.wait_for_cancellation(run_id))
            await asyncio.sleep(0.2)
            (signals / "ready").write_text("ready")
            await wait_until(lambda: (signals / "redis-down").exists(), seconds=90)
            pending_write = asyncio.create_task(store.checkpoint_history(run_id, '{"messages":[]}'))
            await asyncio.sleep(0.5)
            assert not observer.done()
            assert not pending_write.done()
            (signals / "write-waiting").write_text("waiting")
            await wait_until(lambda: (signals / "redis-up").exists(), seconds=30)
            await asyncio.wait_for(pending_write, 15)
            assert not observer.done()
            _, created = await store.create_run_once(run_id)
            assert not created
            await store.request_cancellation(run_id, CancelRunRequest(reason="user_cancelled"))
            intent = await asyncio.wait_for(observer, 15)
            assert intent.reason == "user_cancelled"
            await store.finalize_cancellation(run_id, intent)
            assert (await store.get_run(run_id)).status == "cancelled"
            assert await store.get_history_checkpoint(run_id) == {"messages": []}
            (signals / "passed").write_text("passed")
        finally:
            if observer is not None and not observer.done():
                observer.cancel()
            keys = [key async for key in redis.scan_iter(match=prefix + ":*")]
            if keys:
                await redis.delete(*keys)
            await redis.aclose()

    asyncio.run(scenario())
