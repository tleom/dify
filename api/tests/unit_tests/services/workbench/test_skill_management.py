from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from werkzeug.exceptions import BadRequest, Conflict

from services.workbench import resources


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> Mock:
    owner = Mock(return_value="owner-workspace")
    monkeypatch.setattr(resources, "ensure_workspace", owner)
    monkeypatch.setattr(resources.redis_client, "lock", Mock(side_effect=lambda *_args, **_kwargs: nullcontext()))
    result = Mock(return_value={"id": "report"})
    result.owner = owner
    monkeypatch.setattr(resources, "manager", result)
    return result


@pytest.mark.parametrize("operation", ["skill_update", "skill_uninstall", "skill_pin"])
def test_skill_mutations_use_server_resolved_owner(manager: Mock, operation: str) -> None:
    payload = {
        "operation": operation,
        "name": "report",
        "version": "viewed-version",
        "content": "---\nname: report\ndescription: Report writing\n---\n# Updated",
        "pinned": True,
    }
    assert resources.mutate("tenant", "account", payload) == {"id": "report"}
    manager.owner.assert_called_once_with("tenant", "account")
    manager.assert_called_once_with("owner-workspace", "personal-resources", payload)


@pytest.mark.parametrize(
    "content",
    [
        None,
        "missing metadata",
        "---\nname: changed\ndescription: Description\n---",
        "---\nname: report\ndescription: Description\n---\n" + "中" * 22000,
    ],
    ids=["missing", "no-header", "renamed", "over-limit"],
)
def test_invalid_skill_edits_do_not_reach_manager(manager: Mock, content: str | None) -> None:
    with pytest.raises(BadRequest):
        resources.mutate(
            "tenant", "account", {"operation": "skill_update", "name": "report", "version": "v1", "content": content}
        )
    manager.assert_not_called()


@pytest.mark.parametrize("operation", ["skill_update", "skill_uninstall"])
def test_edits_and_uninstalls_require_a_viewed_revision(manager: Mock, operation: str) -> None:
    with pytest.raises(BadRequest):
        resources.mutate("tenant", "account", {"operation": operation, "name": "report"})
    manager.assert_not_called()


def test_stale_skill_revision_is_a_conflict(manager: Mock) -> None:
    manager.return_value = {"conflict": True}
    with pytest.raises(Conflict):
        resources.mutate("tenant", "account", {"operation": "skill_uninstall", "name": "report", "version": "v1"})
