"""Owned execution, concurrent editor writes and lost responses for personal memory."""

from collections.abc import Generator
from contextlib import contextmanager
from hashlib import sha256
from typing import TypedDict
from uuid import uuid4

import pytest
from werkzeug.exceptions import BadRequest, Conflict, Forbidden, NotFound

from services.workbench import resources
from tests.unit_tests.services.workbench.test_followups import Queue, queue_fixture

queue = pytest.fixture(queue_fixture)


class MemoryStore(TypedDict):
    content: str
    version: str | None
    writes: list[dict[str, str | None]]
    warnings: list[str]
    locked: bool


MemoryFixture = tuple[resources.AgentMemoryPayload, MemoryStore]


@pytest.fixture
def memory(queue: Queue, monkeypatch: pytest.MonkeyPatch) -> MemoryFixture:
    run = queue.send("整理用户偏好")
    queue.running(run["id"])
    backend_run_id = queue.get(run["id"]).backend_run_id
    assert backend_run_id is not None
    payload = resources.AgentMemoryPayload(
        tenant_id=queue.owner[0],
        account_id=queue.owner[1],
        app_id=queue.app_id,
        workbench_run_id=run["id"],
        backend_run_id=backend_run_id,
        content="用中文回答",
        version=None,
    )
    store: MemoryStore = {"content": "", "version": None, "writes": [], "warnings": [], "locked": False}

    def workspace(tenant: str, account: str) -> str:
        assert (tenant, account) == queue.owner
        return "owned-workspace"

    @contextmanager
    def lock(name: str, **_kwargs: object) -> Generator[None]:
        assert name == "workbench:resources:owned-workspace"
        store["locked"] = True
        try:
            yield
        finally:
            store["locked"] = False

    def manager(identifier: str, operation: str, data: dict[str, str | None]) -> dict[str, object]:
        assert identifier == "owned-workspace"
        assert operation == "personal-resources"
        if data["operation"] == "list":
            return {
                "memory": {k: store[k] for k in ("content", "version")},
                "skills": [],
                "warnings": store["warnings"],
            }
        assert store["locked"]
        if data["version"] != store["version"]:
            return {"conflict": True}
        content = data["content"]
        assert isinstance(content, str)
        store["content"] = content
        store["version"] = sha256(content.encode()).hexdigest()
        store["writes"].append(data.copy())
        return {k: store[k] for k in ("content", "version")}

    monkeypatch.setattr(resources, "ensure_workspace", workspace)
    monkeypatch.setattr(resources.redis_client, "lock", lock)
    monkeypatch.setattr(resources, "manager", manager)
    return payload, store


def test_memory_retry_is_noop_and_concurrent_editor_change_requires_merge(memory: MemoryFixture) -> None:
    payload, store = memory
    first = resources.agent_memory_update(payload)
    assert resources.agent_memory_update(payload) == first
    assert len(store["writes"]) == 1
    resources.mutate(
        payload.tenant_id,
        payload.account_id,
        {
            "operation": "memory_update",
            "content": "用中文回答\n报告先写结论",
            "version": first["version"],
        },
    )
    with pytest.raises(Conflict, match="其他会话"):
        resources.agent_memory_update(
            payload.model_copy(update={"content": "用中文回答\n金额保留两位小数", "version": first["version"]})
        )
    assert store["content"] == "用中文回答\n报告先写结论"
    merged = resources.agent_memory_update(
        payload.model_copy(
            update={
                "content": "用中文回答\n报告先写结论\n金额保留两位小数",
                "version": store["version"],
            }
        )
    )
    assert "报告先写结论" in merged["content"]
    assert len(store["writes"]) == 3


@pytest.mark.parametrize("field", ["tenant_id", "account_id", "app_id", "workbench_run_id", "backend_run_id"])
def test_memory_rejects_other_owner_app_or_execution(memory: MemoryFixture, field: str) -> None:
    payload, store = memory
    with pytest.raises((Forbidden, NotFound)):
        resources.agent_memory_update(payload.model_copy(update={field: str(uuid4())}))
    assert store["writes"] == []


def test_memory_rechecks_execution_after_waiting_for_account_lock(
    memory: MemoryFixture, queue: Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, store = memory

    @contextmanager
    def lock(*_args: object, **_kwargs: object) -> Generator[None]:
        queue.finish(payload.workbench_run_id)
        yield

    monkeypatch.setattr(resources.redis_client, "lock", lock)
    with pytest.raises(Forbidden):
        resources.agent_memory_update(payload)
    assert store["writes"] == []


def test_memory_rejects_unreadable_snapshot_and_utf8_byte_overflow(memory: MemoryFixture) -> None:
    payload, store = memory
    store["warnings"] = ["memory.md 不是有效 UTF-8，未载入模型"]
    with pytest.raises(Conflict, match="无法完整读取"):
        resources.agent_memory_update(payload)
    store["warnings"] = []
    with pytest.raises(BadRequest, match="64 KiB"):
        resources.agent_memory_update(payload.model_copy(update={"content": "中" * 22000}))
    assert store["writes"] == []
