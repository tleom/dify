"""Real owned-run state guards the read-only manager operation."""

from uuid import uuid4

import pytest
from werkzeug.exceptions import Conflict, Forbidden, NotFound

from services.workbench import control, planning
from tests.unit_tests.services.workbench.test_followups import queue_fixture

queue = pytest.fixture(queue_fixture)


def setup_inspection(queue, monkeypatch):
    result = control.issue(*queue.owner, queue.chat_id, command="/plan 核对附件并制定方案", request_key="planning")
    identifier = result["run"]["id"]
    queue.running(identifier)
    monkeypatch.setattr(planning, "ensure_workspace", lambda *_args: "owner-workspace")
    calls = []
    monkeypatch.setattr(
        planning,
        "manager",
        lambda *args, **kwargs: (
            calls.append((args, kwargs))
            or ({"ticket": "inspection-generation"} if args[1] == "plan-admission" else {"output": "read"})
        ),
    )
    return planning.AgentPlanInspectPayload(
        tenant_id=queue.owner[0],
        account_id=queue.owner[1],
        app_id=queue.app_id,
        workbench_run_id=identifier,
        backend_run_id=queue.get(identifier).backend_run_id,
        request_key="inspect",
        script="ls",
    ), calls


def test_inspection_resolves_binding_from_owned_chat_and_propagates_execution(queue, monkeypatch):
    payload, calls = setup_inspection(queue, monkeypatch)
    assert planning.inspect(payload) == {"output": "read"}
    assert calls[0][0][1] == "plan-admission"
    args, kwargs = calls[1]
    assert args[:2] == ("owner-workspace", "plan-inspect")
    assert args[2]["binding_id"] == queue.chat_id
    assert args[2]["execution_id"] == payload.backend_run_id
    assert args[2]["admission_ticket"] == "inspection-generation"
    assert kwargs["timeout"] > payload.timeout


@pytest.mark.parametrize("field", ["tenant_id", "account_id", "app_id", "backend_run_id"])
def test_inspection_rejects_changed_identity_without_manager_io(queue, monkeypatch, field):
    payload, calls = setup_inspection(queue, monkeypatch)
    setattr(payload, field, str(uuid4()))
    with pytest.raises((Forbidden, NotFound)):
        planning.inspect(payload)
    assert calls == []


def test_inspection_rechecks_mode_after_workspace_startup(queue, monkeypatch):
    payload, calls = setup_inspection(queue, monkeypatch)

    def start(*_args):
        control.issue(*queue.owner, queue.chat_id, command="/plan off", request_key="leave")
        return "workspace"

    monkeypatch.setattr(planning, "ensure_workspace", start)
    with pytest.raises(Conflict, match="计划阶段已结束"):
        planning.inspect(payload)
    assert [call[0][1] for call in calls] == ["plan-admission"]


def test_inspection_rechecks_execution_after_getting_manager_ticket(queue, monkeypatch):
    payload, calls = setup_inspection(queue, monkeypatch)

    def manager(*args, **kwargs):
        calls.append((args, kwargs))
        assert args[1] == "plan-admission"
        queue.finish(payload.workbench_run_id, "cancelled")
        return {"ticket": "old-generation"}

    monkeypatch.setattr(planning, "manager", manager)
    with pytest.raises(Forbidden):
        planning.inspect(payload)
    assert len(calls) == 1


def test_inner_http_route_validates_identity_payload_and_serializes_preview_schema(monkeypatch, config_overrides):
    from flask import Flask

    from controllers.inner_api import bp
    from controllers.inner_api.agent import workbench_control as endpoint

    config_overrides(PLUGIN_DAEMON_KEY="test-daemon", INNER_API_KEY_FOR_PLUGIN="test-inner")
    calls = []

    def inspect(value):
        calls.append(value)
        return {
            "output": "read",
            "output_path": "/tmp/output.log",
            "output_truncated": False,
            "exit_code": 0,
            "timed_out": False,
            "previews": [{"path": "/tmp/page.png", "media_type": "image/png", "data": "aW1hZ2U="}],
        }

    monkeypatch.setattr(endpoint, "inspect", inspect)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(bp)
    client = app.test_client()
    url = "/inner/api/agent/workbench/plan/inspect"
    headers = {"X-Inner-Api-Key": "test-inner"}
    body = {
        "tenant_id": "tenant",
        "account_id": "owner",
        "app_id": "app",
        "workbench_run_id": "run",
        "backend_run_id": "execution",
        "request_key": "call",
        "script": "ls",
    }
    assert client.post(url, json=body).status_code == 404
    assert client.post(url, json={**body, "timeout": 61}, headers=headers).status_code == 400
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 200
    assert response.json["previews"][0]["media_type"] == "image/png"
    assert response.json["warnings"] == []
    assert len(calls) == 1
    assert isinstance(calls[0], planning.AgentPlanInspectPayload)
    from controllers.inner_api import api

    with app.test_request_context():
        operation = api.__schema__["paths"]["/agent/workbench/plan/inspect"]["post"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/AgentPlanInspectResponse")
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/AgentPlanInspectPayload"
    )
