"""Manager scheduling, authorization and durable retention with bounded fake IO."""

import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture
def manager(tmp_path, monkeypatch):
    base = Path(__file__).parents[1] / "sandbox-manager"
    monkeypatch.syspath_prepend(str(base))
    monkeypatch.setenv("WORKBENCH_SANDBOX_MANAGER_TOKEN", "test-manager-token")
    monkeypatch.setenv("WORKBENCH_MANAGER_STATE", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "tested_mcp_manager", base / "server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import mcp_runtime

    calls, entered = [], threading.Event()

    class Worker:
        def __init__(self, container, name, version, timeout):
            self.version, self.ready = version, None
            self.process = SimpleNamespace(poll=lambda: None)
            self.closed, self.pending = threading.Event(), None

        def response(self, cancelled=None, timeout=None):
            if self.ready is None:
                return {"ready": True}
            if self.pending.get("tool") == "slow":
                assert self.closed.wait(4), "test did not stop the in-flight request"
                return {"error": "stopped", "uncertain": True}
            return {"result": {"value": "ok"}}

        def send(self, payload):
            self.pending = payload
            calls.append(payload)
            entered.set()

        def close(self):
            self.closed.set()

    def docker(*args, stdin=None, **kwargs):
        if args[0] == "ps":
            # This fixture has MCP workers but no planning containers.
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "inspect":
            return SimpleNamespace(
                returncode=0, stdout=json.dumps([{"State": {"Running": True}}])
            )
        payload = json.loads(stdin)
        result = {"version": "v1", "enabled": True, "timeout": 5}
        if "binding_id" in payload:
            result = {"stopped": 0}
        elif payload["operation"] == "mcp_list":
            result = {"mcp": []}
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))

    monkeypatch.setattr(module, "ensure", lambda _: None)
    monkeypatch.setattr(module, "docker", docker)
    monkeypatch.setattr(mcp_runtime, "Worker", Worker)
    monkeypatch.setattr(module, "authorize_mcp", lambda _: True)
    yield module, mcp_runtime, calls, entered
    for key, _ in list(mcp_runtime.WORKERS):
        mcp_runtime.invalidate(key)
    assert not mcp_runtime.ACTIVE


def payload(key, binding, *, tool="slow", request="call-1"):
    return {
        "operation": "mcp_call",
        "name": "demo",
        "version": "v1",
        "manifest_version": "m1",
        "tool": tool,
        "arguments": {},
        "request_key": request,
        "authorization": {"workspace_id": key, "binding_id": binding},
    }


def test_stop_and_catalog_do_not_wait_for_business_call_and_queued_calls_reauthorize(
    manager, monkeypatch
):
    module, runtime, calls, entered = manager
    key, first, second = [str(uuid4()) for _ in range(3)]
    allowed = {first: True, second: True}
    monkeypatch.setattr(
        module, "authorize_mcp", lambda auth: allowed[auth["binding_id"]]
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        active = pool.submit(module.operation, key, "personal-mcp", payload(key, first))
        assert entered.wait(2)
        queued = pool.submit(
            module.operation,
            key,
            "personal-mcp",
            payload(key, second, tool="fast", request="call-2"),
        )
        deadline = time.monotonic() + 2
        while len(runtime.ACTIVE) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(runtime.ACTIVE) == 2
        assert module.operation(key, "personal-mcp", {"operation": "mcp_list"}) == {
            "mcp": []
        }
        allowed[second] = False
        started = time.monotonic()
        assert module.operation(key, "stop-binding", {"binding_id": first})["fenced"]
        assert time.monotonic() - started < 1
        assert active.result()["uncertain"]
        assert "失效" in queued.result()["error"]
    assert len(calls) == 1


def test_stop_cancels_only_its_binding_and_other_server_can_complete(manager):
    module, _, calls, entered = manager
    key, first, second = [str(uuid4()) for _ in range(3)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(module.operation, key, "personal-mcp", payload(key, first))
        assert entered.wait(2)
        other = {**payload(key, second, tool="fast", request="call-2"), "name": "other"}
        assert module.operation(key, "personal-mcp", other)["result"]["value"] == "ok"
        module.operation(key, "stop-binding", {"binding_id": second})
        assert not active.done()
        module.operation(key, "stop-binding", {"binding_id": first})
        assert active.result()["uncertain"]
    assert len(calls) == 2


def test_stop_during_final_authorization_prevents_dispatch(manager, monkeypatch):
    module, _, calls, _ = manager
    key, binding = str(uuid4()), str(uuid4())
    checking, resume = threading.Event(), threading.Event()
    checks = 0

    def authorize(_):
        nonlocal checks
        checks += 1
        if checks == 2:
            checking.set()
            assert resume.wait(3)
        return True

    monkeypatch.setattr(module, "authorize_mcp", authorize)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            module.operation,
            key,
            "personal-mcp",
            payload(key, binding, tool="fast"),
        )
        try:
            assert checking.wait(2)
            assert module.operation(key, "stop-binding", {"binding_id": binding})[
                "fenced"
            ]
        finally:
            resume.set()
        assert "未执行" in pending.result()["error"]
    assert not calls


def test_retention_limits_keep_tombstones_and_request_identity(manager):
    module, _, _, _ = manager
    from mcp_journal import Journal

    journal = Journal(module.STATE, owner_bytes=150, total_bytes=200, max_records=4)
    stored = []
    for owner, request in (("a", "1"), ("a", "2"), ("b", "3")):
        body = {"request_key": request, "tool": "query"}
        digest, previous = journal.begin(owner, body)
        assert previous is None
        journal.finish(digest, {"result": "x" * 100})
        stored.append((owner, body, digest))
    with journal.connection() as db:
        assert db.execute("SELECT sum(length(result)) FROM calls").fetchone()[0] <= 200
    assert journal.begin(*stored[0][:2])[1]["uncertain"]
    assert journal.begin(*stored[1][:2])[1]["uncertain"]
    assert journal.begin(*stored[2][:2])[1]["result"] == "x" * 100
    journal.ttl = -1
    journal.prune()
    restarted = Journal(module.STATE)
    assert restarted.begin(*stored[2][:2])[1]["uncertain"]
    assert (
        "不同参数"
        in restarted.begin("b", {"request_key": "3", "tool": "changed"})[1]["error"]
    )
    assert journal.begin("c", {"request_key": "4"})[1] is None
    assert "容量" in journal.begin("c", {"request_key": "5"})[1]["error"]


def test_legacy_result_is_compacted_without_replay(manager):
    module, _, _, _ = manager
    from mcp_journal import Journal

    journal = Journal(module.STATE)
    body = {"request_key": "old-call"}
    digest, fingerprint = journal.identity("owner", body)
    path = module.STATE / ("mcp-" + digest)
    path.write_text(
        json.dumps({"fingerprint": fingerprint, "result": {"content": "x" * 10000}})
    )
    journal.prune()
    assert path.stat().st_size < 100
    assert journal.begin("owner", body)[1]["uncertain"]
