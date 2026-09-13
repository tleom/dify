from unittest.mock import MagicMock

import pytest
from werkzeug.exceptions import Forbidden

from core.rbac import RBACPermission, RBACResourceScope
from services.workbench import authorization


@pytest.mark.parametrize("allowed", [True, False])
def test_agent_run_permission_uses_exact_tenant_account_and_agent(
    monkeypatch: pytest.MonkeyPatch, allowed: bool
) -> None:
    check = MagicMock(return_value=allowed)
    monkeypatch.setattr(authorization.RBACService.CheckAccess, "check", check)

    if allowed:
        authorization.require_agent_run("tenant-1", "account-1", "agent-1")
    else:
        with pytest.raises(Forbidden):
            authorization.require_agent_run("tenant-1", "account-1", "agent-1")

    check.assert_called_once_with(
        "tenant-1",
        "account-1",
        scene=RBACPermission.AGENT_TEST_AND_RUN,
        resource_type=RBACResourceScope.AGENT,
        resource_id="agent-1",
    )


@pytest.mark.parametrize(
    "enabled,owner,allowed,expected",
    [
        (False, "someone-else", False, True),
        (True, "account-1", False, True),
        (True, "someone-else", True, True),
        (True, "someone-else", False, False),
    ],
)
def test_dataset_permission_preserves_maintainer_and_rbac_policy(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, owner: str, allowed: bool, expected: bool
) -> None:
    monkeypatch.setattr(authorization.dify_config, "RBAC_ENABLED", enabled)
    maintainer = MagicMock(return_value=owner)
    check = MagicMock(return_value=allowed)
    monkeypatch.setattr(authorization.RBACResourceService, "get_dataset_maintainer", maintainer)
    monkeypatch.setattr(authorization.RBACService.CheckAccess, "check", check)

    assert authorization.can_retrieve_dataset("tenant-1", "account-1", "dataset-1") is expected

    if enabled:
        maintainer.assert_called_once_with("tenant-1", "dataset-1")
    else:
        maintainer.assert_not_called()
    if enabled and owner != "account-1":
        check.assert_called_once_with(
            "tenant-1",
            "account-1",
            scene=RBACPermission.DATASET_RETRIEVAL_RECALL,
            resource_type=RBACResourceScope.DATASET,
            resource_id="dataset-1",
        )
    else:
        check.assert_not_called()
