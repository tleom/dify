from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from werkzeug.exceptions import Forbidden

from core.rbac import RBACPermission, RBACResourceScope
from models.agent_config_entities import AgentSoulConfig
from services.workbench import service
from tests.unit_tests.config_override import apply_config_overrides


@pytest.mark.parametrize("allowed", [True, False])
def test_template_enforces_native_agent_run_permission(monkeypatch, allowed):
    monkeypatch.setattr("services.workbench.knowledge.available_sets", lambda *args: [])
    apply_config_overrides(
        monkeypatch, RBAC_ENABLED=True, WORKBENCH_AGENT_TEMPLATES={"tenant": "agent"}
    )
    agent = SimpleNamespace(
        id="agent",
        active_config_snapshot_id="snapshot",
        active_config_is_published=True,
    )
    snapshot = SimpleNamespace(
        id="snapshot", config_snapshot_dict=AgentSoulConfig().model_dump(mode="json")
    )

    @contextmanager
    def session():
        yield SimpleNamespace(
            scalar=lambda statement: agent, get=lambda model, identity: snapshot
        )

    monkeypatch.setattr(service, "authorize", lambda *args: None)
    monkeypatch.setattr(service.session_factory, "create_session", session)
    monkeypatch.setattr(
        service,
        "AgentRosterService",
        lambda session: SimpleNamespace(
            get_agent_runtime_app_model=lambda **kwargs: SimpleNamespace(id="app")
        ),
    )
    monkeypatch.setattr(
        "core.workflow.nodes.agent_v2.dify_tools_builder.WorkflowAgentDifyToolsBuilder.expand_provider_entries",
        lambda self, **kwargs: [],
    )
    monkeypatch.setattr(
        "services.skill_management_service.SkillManagementService.list_runtime_agent_skills",
        lambda self, **kwargs: [],
    )
    observed = []

    def check(tenant_id, account_id, **kwargs):
        observed.append((tenant_id, account_id, kwargs))
        return allowed

    monkeypatch.setattr(
        "services.enterprise.rbac_service.RBACService.CheckAccess.check", check
    )
    if allowed:
        assert service.template("tenant", "account")["agent_id"] == "agent"
    else:
        with pytest.raises(Forbidden):
            service.template("tenant", "account")
    assert observed == [
        (
            "tenant",
            "account",
            {
                "scene": RBACPermission.AGENT_TEST_AND_RUN,
                "resource_type": RBACResourceScope.AGENT,
                "resource_id": "agent",
            },
        )
    ]
