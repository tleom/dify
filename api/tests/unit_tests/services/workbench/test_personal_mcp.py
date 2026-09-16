"""MCP owner and execution fences exercised against real persisted Workbench runs."""

import json
from unittest.mock import Mock
from uuid import uuid4

import pytest
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from models.agent import AgentConfigVersionKind, AgentWorkspaceBinding
from models.enums import ConversationFromSource
from models.model import AppMode, Conversation
from models.workbench import WorkbenchChat, WorkbenchRun
from services.workbench import personal_mcp
from services.workbench.mentions import resolve_mentions
from tests.unit_tests.services.workbench.test_followups import Queue, queue_fixture

queue = pytest.fixture(queue_fixture)


def declaration():
    return {
        "id": "personal:mcp:demo:query",
        "plugin_id": "personal:mcp:demo",
        "server_id": "demo",
        "provider_name": "个人查询",
        "runtime_name": personal_mcp.runtime_name("demo", "query"),
        "tool_name": "query",
        "version": "config-v1",
        "manifest_version": "tools-v1",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    }


@pytest.fixture
def execution(queue: Queue, monkeypatch):
    current = [declaration()]
    monkeypatch.setattr(personal_mcp, "catalog", lambda *_: current)
    run = queue.send("查询", resource_mentions={"tools": [current[0]["id"]]})
    queue.running(run["id"])
    record = queue.get(run["id"])
    payload = personal_mcp.AgentMCPPayload(
        tenant_id=queue.owner[0],
        account_id=queue.owner[1],
        app_id=queue.app_id,
        workbench_run_id=run["id"],
        backend_run_id=record.backend_run_id,
        operation="call",
        tool_id=current[0]["id"],
        arguments={"query": "甲"},
        request_key="call-1",
    )
    workspace, conversation, binding = [str(uuid4()) for _ in range(3)]
    with queue.factory.begin() as session:
        chat = session.get(WorkbenchChat, queue.chat_id)
        chat.conversation_id = conversation
        session.add(
            Conversation(
                id=conversation,
                app_id=queue.app_id,
                mode=AppMode.AGENT,
                name="MCP",
                from_source=ConversationFromSource.CONSOLE,
                from_account_id=queue.owner[1],
                agent_workspace_binding_id=binding,
                _inputs={},
            )
        )
        session.add(
            AgentWorkspaceBinding(
                id=binding,
                tenant_id=queue.owner[0],
                app_id=queue.app_id,
                workspace_id=workspace,
                agent_id=chat.agent_id,
                agent_config_version_id=str(uuid4()),
                agent_config_version_kind=AgentConfigVersionKind.SNAPSHOT,
                backend_binding_ref=f"wb:{binding}:{workspace}",
            )
        )
    owner = Mock(return_value=workspace)
    transport = Mock(return_value={"result": {"content": [{"type": "text", "text": "结果"}], "isError": False}})
    monkeypatch.setattr(personal_mcp, "ensure_workspace", owner)
    monkeypatch.setattr(personal_mcp, "manager", transport)
    return payload, current, owner, transport


def test_run_freezes_personal_tools_and_invocation_resolves_workspace_on_server(queue, execution):
    payload, _, owner, transport = execution
    frozen = json.loads(queue.get(payload.workbench_run_id).payload)["personal_mcp_tools"]
    assert frozen == [declaration()]
    assert personal_mcp.agent_operation(payload)["result"]["isError"] is False
    owner.assert_called_with(*queue.owner)
    workspace, action, request = transport.call_args.args
    assert workspace == owner.return_value
    assert action == "personal-mcp"
    assert request["name"] == "demo"
    assert request["tool"] == "query"
    assert payload.workbench_run_id in request["request_key"]
    assert payload.backend_run_id in request["request_key"]
    assert "credentials" not in request
    authorization = personal_mcp.MCPAuthorizationPayload.model_validate(request["authorization"])
    assert personal_mcp.authorize_call(authorization) == {"authorized": True}


@pytest.mark.parametrize("field", ["tenant_id", "account_id", "app_id", "backend_run_id", "workbench_run_id"])
def test_foreign_owner_or_superseded_execution_cannot_access_mcp(execution, field):
    payload, _, owner, transport = execution
    with pytest.raises((Forbidden, NotFound)):
        personal_mcp.agent_operation(payload.model_copy(update={field: str(uuid4())}))
    owner.assert_not_called()
    transport.assert_not_called()


@pytest.mark.parametrize("change", ["disabled", "version", "manifest_version"])
def test_disabled_or_changed_tools_cannot_run_with_stale_schema(execution, change):
    payload, current, _, transport = execution
    if change == "disabled":
        current.clear()
    else:
        current[0] = {**current[0], change: "changed"}
    with pytest.raises(Forbidden):
        personal_mcp.agent_operation(payload)
    transport.assert_not_called()


@pytest.mark.parametrize("change", ["cancelled", "execution_replaced"])
def test_fence_is_rechecked_after_catalog_io(queue, execution, monkeypatch, change):
    payload, current, _, transport = execution

    def catalog(*_args, **_kwargs):
        with queue.factory.begin() as session:
            run = session.get(WorkbenchRun, payload.workbench_run_id)
            if change == "cancelled":
                run.status = "cancelled"
            else:
                run.backend_run_id = str(uuid4())
        return current

    monkeypatch.setattr(personal_mcp, "catalog", catalog)
    with pytest.raises(Forbidden):
        personal_mcp.agent_operation(payload)
    transport.assert_not_called()


@pytest.mark.parametrize(
    "change",
    ["cancelled", "execution_replaced", "workspace_id", "binding_id", "version"],
)
def test_manager_authorization_rechecks_execution_and_owned_binding(queue, execution, change):
    payload, _, _, transport = execution
    personal_mcp.agent_operation(payload)
    value = dict(transport.call_args.args[2]["authorization"])
    with queue.factory.begin() as session:
        run = session.get(WorkbenchRun, payload.workbench_run_id)
        if change == "cancelled":
            run.status = "cancelled"
        elif change == "execution_replaced":
            run.backend_run_id = str(uuid4())
        else:
            value[change] = str(uuid4())
    with pytest.raises(Forbidden):
        personal_mcp.authorize_call(personal_mcp.MCPAuthorizationPayload.model_validate(value))


def test_manager_authorization_http_requires_credential_and_current_execution(queue, execution, config_overrides):
    from flask import Flask

    from controllers.inner_api.agent.workbench_control import ManagerMCPAuthorization

    payload, _, _, transport = execution
    personal_mcp.agent_operation(payload)
    value = transport.call_args.args[2]["authorization"]
    config_overrides(WORKBENCH_SANDBOX_MANAGER_TOKEN="manager-test-credential")
    app = Flask(__name__)
    app.add_url_rule("/authorize", view_func=ManagerMCPAuthorization().post, methods=["POST"])
    client = app.test_client()
    assert client.post("/authorize", json=value).status_code == 403
    assert client.post("/authorize", json=value, headers={"Authorization": "Bearer wrong"}).status_code == 403
    headers = {"Authorization": "Bearer manager-test-credential"}
    response = client.post("/authorize", json=value, headers=headers)
    assert response.status_code == 200
    assert response.json == {"authorized": True}
    queue.finish(payload.workbench_run_id, "cancelled")
    assert client.post("/authorize", json=value, headers=headers).status_code == 403


def test_public_resource_mutations_cannot_invoke_tools_or_write_manifest(monkeypatch):
    owner = Mock()
    monkeypatch.setattr(personal_mcp, "ensure_workspace", owner)
    for operation in ("mcp_call", "mcp_cache", "mcp_probe"):
        with pytest.raises(BadRequest):
            personal_mcp.mutate("tenant", "account", {"operation": operation})
    owner.assert_not_called()


def test_personal_mcp_mentions_have_separate_groups_and_runtime_names():
    tool = declaration()
    result = resolve_mentions({}, {"tools": [tool["id"]]}, personal_mcp=[tool])
    assert result["mentioned_resources"] == [
        {"kind": "tools", "id": "plugin:mcp:personal:mcp:demo", "name": "个人查询"}
    ]
    assert tool["runtime_name"] in result["mention_prompt"]
    with pytest.raises(ValueError, match="个人 MCP"):
        resolve_mentions({}, {"tools": [tool["id"]]})
