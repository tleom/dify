from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

import pytest

from dify_agent.protocol import CancelRunResponse, DIFY_AGENT_MODEL_LAYER_ID, RunFailureType
from dify_agent.runtime.run_scheduler import RunCancellationConflictError, SchedulerStoppingError
from dify_agent.server.routes.runs import create_runs_router
from dify_agent.server.schemas import RunRecord
from dify_agent.storage.redis_run_store import RunNotFoundError


class FakeScheduler:
    async def create_run(self, request: object) -> object:
        del request
        return RunRecord(run_id="run-1", status="running")

    async def cancel_run(self, run_id: str, request: object) -> CancelRunResponse:
        del request
        return CancelRunResponse(run_id=run_id, status="cancelled")


class FakeStore:
    pass


@pytest.mark.parametrize("status", ["running", "cancelled"])
def test_fence_returns_history_only_after_execution_stops(status: str) -> None:
    from fastapi import FastAPI

    ticket = "c2890d1b-cd23-4e32-a78a-f9b070a49de2"
    store = AsyncMock()
    store.fence_run.return_value = status
    store.redis.get.return_value = None
    store.get_fenced_state.return_value = {}
    store.get_history_checkpoint.return_value = {"messages": []}
    app = FastAPI()
    app.include_router(create_runs_router(lambda: store, lambda: FakeScheduler()))  # pyright: ignore[reportArgumentType]
    client = TestClient(app)
    response = client.post(f"/runs/{ticket}/fence", json={})
    assert response.status_code == 200
    assert response.json() == {
        "run_id": ticket,
        "status": status,
        "history": {"messages": []} if status != "running" else None,
        "steering_delivered_ids": None,
    }
    assert store.get_history_checkpoint.await_count == int(status != "running")
    assert client.post("/runs/not-a-ticket/fence", json={}).status_code == 400
    schema = client.get("/openapi.json").json()
    assert "history" in schema["components"]["schemas"]["FenceRunResponse"]["properties"]


def test_fence_returns_context_only_after_native_execution_is_terminal(monkeypatch):
    from uuid import uuid4
    from fastapi import FastAPI
    from dify_agent.runtime import workbench_recovery

    class Store:
        status = "running"

        async def fence_run(self, run_id):
            return self.status

        async def get_fenced_state(self, run_id):
            assert self.status != "running"
            return {"history": {"messages": []}, "steering_delivered_ids": ["seen"]}

    async def recover(store, run_id, status):
        return status

    monkeypatch.setattr(workbench_recovery, "recover_fenced_run", recover)
    store = Store()
    app = FastAPI()
    app.include_router(create_runs_router(lambda: store, lambda: FakeScheduler()))
    client = TestClient(app)
    path = f"/runs/{uuid4()}/fence"
    assert client.post(path).json()["history"] is None
    store.status = "cancelled"
    response = client.post(path)
    assert response.status_code == 200
    assert response.json()["history"] == {"messages": []}
    assert response.json()["steering_delivered_ids"] == ["seen"]


def test_get_run_status_returns_failure_type() -> None:
    from fastapi import FastAPI

    class FailedRunStore:
        async def get_run(self, run_id: str) -> RunRecord:
            return RunRecord(
                run_id=run_id,
                status="failed",
                error="run limit reached",
                error_type=RunFailureType.AGENT_RUN_LIMIT_EXCEEDED,
            )

    app = FastAPI()
    app.include_router(
        create_runs_router(lambda: FailedRunStore(), lambda: FakeScheduler())  # pyright: ignore[reportArgumentType]
    )
    response = TestClient(app).get("/runs/run-1")

    assert response.status_code == 200
    assert response.json()["error"] == "run limit reached"
    assert response.json()["error_type"] == "agent_run_limit_exceeded"


def test_create_run_accepts_effectively_blank_user_prompt_list() -> None:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(
        create_runs_router(lambda: FakeStore(), lambda: FakeScheduler())  # pyright: ignore[reportArgumentType]
    )
    client = TestClient(app)

    response = client.post(
        "/runs",
        json={
            "composition": {
                "schema_version": 1,
                "layers": [{"name": "prompt", "type": "plain.prompt", "config": {"user": ["", "   "]}}],
            }
        },
    )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1", "status": "running"}


def test_create_run_returns_running_from_scheduler() -> None:
    from fastapi import FastAPI

    class CapturingScheduler:
        async def create_run(self, request: object) -> RunRecord:
            del request
            return RunRecord(run_id="run-1", status="running")

    app = FastAPI()
    app.include_router(
        create_runs_router(lambda: FakeStore(), lambda: CapturingScheduler())  # pyright: ignore[reportArgumentType]
    )
    client = TestClient(app)

    response = client.post(
        "/runs",
        json={
            "composition": {
                "schema_version": 1,
                "layers": [{"name": "prompt", "type": "plain.prompt", "config": {"user": "hello"}}],
            }
        },
    )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1", "status": "running"}


def test_cancel_run_endpoint_returns_scheduler_result() -> None:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(
        create_runs_router(lambda: FakeStore(), lambda: FakeScheduler())  # pyright: ignore[reportArgumentType]
    )
    client = TestClient(app)

    response = client.post("/runs/run-1/cancel", json={"reason": "user_cancelled"})

    assert response.status_code == 200
    assert response.json() == {"run_id": "run-1", "status": "cancelled"}


def test_cancel_run_endpoint_maps_conflict() -> None:
    from fastapi import FastAPI

    class ConflictingScheduler(FakeScheduler):
        async def cancel_run(self, run_id: str, request: object) -> CancelRunResponse:
            del run_id, request
            raise RunCancellationConflictError("run already finished with status 'succeeded'")

    app = FastAPI()
    app.include_router(
        create_runs_router(lambda: FakeStore(), lambda: ConflictingScheduler())  # pyright: ignore[reportArgumentType]
    )
    client = TestClient(app)

    response = client.post("/runs/run-1/cancel", json={})

    assert response.status_code == 409
    assert "already finished" in response.json()["detail"]


def test_cancel_run_endpoint_maps_missing_run() -> None:
    from fastapi import FastAPI

    class MissingRunScheduler(FakeScheduler):
        async def cancel_run(self, run_id: str, request: object) -> CancelRunResponse:
            del request
            raise RunNotFoundError(run_id)

    app = FastAPI()
    app.include_router(
        create_runs_router(lambda: FakeStore(), lambda: MissingRunScheduler())  # pyright: ignore[reportArgumentType]
    )
    client = TestClient(app)

    response = client.post("/runs/missing/cancel", json={})

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}


def test_create_run_accepts_valid_full_plugin_graph() -> None:
    from fastapi import FastAPI

    class CapturingScheduler:
        async def create_run(self, request: object) -> RunRecord:
            del request
            return RunRecord(run_id="run-1", status="running")

    app = FastAPI()
    app.include_router(
        create_runs_router(lambda: FakeStore(), lambda: CapturingScheduler())  # pyright: ignore[reportArgumentType]
    )
    client = TestClient(app)

    response = client.post(
        "/runs",
        json={
            "composition": {
                "schema_version": 1,
                "layers": [
                    {"name": "prompt", "type": "plain.prompt", "config": {"user": "hello"}},
                    {
                        "name": "execution-context-renamed",
                        "type": "dify.execution_context",
                        "config": {
                            "tenant_id": "tenant-1",
                            "user_from": "account",
                            "agent_mode": "workflow_run",
                            "invoke_from": "service-api",
                        },
                    },
                    {
                        "name": DIFY_AGENT_MODEL_LAYER_ID,
                        "type": "dify.plugin.llm",
                        "deps": {"execution_context": "execution-context-renamed"},
                        "config": {
                            "plugin_id": "langgenius/openai",
                            "model_provider": "openai",
                            "model": "gpt-4o-mini",
                            "credentials": {"api_key": "secret"},
                            "model_settings": {"temperature": 0.2},
                        },
                    },
                ],
            }
        },
    )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1", "status": "running"}


def test_create_run_accepts_unknown_layer_exit_signal_request() -> None:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(
        create_runs_router(lambda: FakeStore(), lambda: FakeScheduler())  # pyright: ignore[reportArgumentType]
    )
    client = TestClient(app)

    response = client.post(
        "/runs",
        json={
            "composition": {
                "schema_version": 1,
                "layers": [{"name": "prompt", "type": "plain.prompt", "config": {"user": "hello"}}],
            },
            "on_exit": {"layers": {"missing": "delete"}},
        },
    )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1", "status": "running"}


def test_create_run_accepts_closed_session_snapshot_request() -> None:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(
        create_runs_router(lambda: FakeStore(), lambda: FakeScheduler())  # pyright: ignore[reportArgumentType]
    )
    client = TestClient(app)

    response = client.post(
        "/runs",
        json={
            "composition": {
                "schema_version": 1,
                "layers": [{"name": "prompt", "type": "plain.prompt", "config": {"user": "hello"}}],
            },
            "session_snapshot": {
                "schema_version": 1,
                "layers": [
                    {
                        "name": "prompt",
                        "lifecycle_state": "closed",
                        "runtime_state": {},
                    }
                ],
            },
        },
    )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1", "status": "running"}


def test_create_run_returns_503_when_scheduler_is_stopping() -> None:
    from fastapi import FastAPI

    class StoppingScheduler:
        async def create_run(self, request: object) -> RunRecord:
            del request
            raise SchedulerStoppingError("run scheduler is shutting down")

    app = FastAPI()
    app.include_router(
        create_runs_router(lambda: FakeStore(), lambda: StoppingScheduler())  # pyright: ignore[reportArgumentType]
    )
    client = TestClient(app)

    response = client.post(
        "/runs",
        json={
            "composition": {
                "schema_version": 1,
                "layers": [{"name": "prompt", "type": "plain.prompt", "config": {"user": "hello"}}],
            }
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "run scheduler is shutting down"


def test_create_run_does_not_map_infrastructure_failure_to_422() -> None:
    from fastapi import FastAPI

    class FailingScheduler:
        async def create_run(self, request: object) -> RunRecord:
            del request
            raise RuntimeError("redis unavailable")

    app = FastAPI()
    app.include_router(
        create_runs_router(lambda: FakeStore(), lambda: FailingScheduler())  # pyright: ignore[reportArgumentType]
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/runs",
        json={
            "composition": {
                "schema_version": 1,
                "layers": [{"name": "prompt", "type": "plain.prompt", "config": {"user": "hello"}}],
            }
        },
    )

    assert response.status_code == 500
