from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from werkzeug.exceptions import Forbidden

from services.workbench import knowledge


def test_catalog_paginates_visible_datasets_and_builds_eager_sets(monkeypatch):
    account = SimpleNamespace(current_tenant_id="tenant", set_tenant_id_with_session=lambda *a, **k: None)
    session = MagicMock()
    session.get.return_value = account
    session.__enter__.return_value = session
    monkeypatch.setattr(knowledge.session_factory, "create_session", lambda: session)
    monkeypatch.setattr(knowledge.dify_config, "RBAC_ENABLED", False)
    calls = []

    def datasets(page, size, *args, **kwargs):
        calls.append(page)
        return [SimpleNamespace(id=f"dataset-{page}", name="知识库", description="资料",
                                retrieval_model={"top_k": 3})], 101

    monkeypatch.setattr(knowledge.DatasetService, "get_datasets", datasets)
    result = knowledge.available_sets("tenant", "account")
    assert calls == [1, 2]
    assert result[0]["datasets"] == [{"id": "dataset-1", "name": "知识库"}]
    assert result[0]["query"]["mode"] == "user_query"
    assert result[0]["retrieval"]["top_k"] == 3


def test_queued_run_rechecks_revoked_dataset_access(monkeypatch):
    monkeypatch.setattr(knowledge, "available_sets", lambda *args: [{"id": "allowed"}])
    soul = SimpleNamespace(knowledge=SimpleNamespace(sets=[SimpleNamespace(datasets=[SimpleNamespace(id="revoked")])]))
    with pytest.raises(Forbidden):
        knowledge.validate_run_knowledge("tenant", "account", soul)
    soul.knowledge.sets[0].datasets[0].id = "allowed"
    knowledge.validate_run_knowledge("tenant", "account", soul)
