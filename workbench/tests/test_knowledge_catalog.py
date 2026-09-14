from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from dify_agent.layers.knowledge.configs import DifyKnowledgeBaseLayerConfig
from services.workbench import knowledge
from werkzeug.exceptions import Forbidden


def test_catalog_paginates_visible_datasets_and_exposes_generated_queries(monkeypatch):
    account = SimpleNamespace(
        current_tenant_id="tenant", set_tenant_id_with_session=lambda *a, **k: None
    )
    session = MagicMock()
    session.get.return_value = account
    session.__enter__.return_value = session
    monkeypatch.setattr(knowledge.session_factory, "create_session", lambda: session)
    monkeypatch.setattr(knowledge.dify_config, "RBAC_ENABLED", False)
    calls = []

    def datasets(page, size, *args, **kwargs):
        calls.append(page)
        return [
            SimpleNamespace(
                id=f"dataset-{page}",
                name="知识库",
                description="资料",
                retrieval_model={"top_k": 3},
            )
        ], 101

    monkeypatch.setattr(knowledge.DatasetService, "get_datasets", datasets)
    result = knowledge.available_sets("tenant", "account")
    assert calls == [1, 2]
    assert result[0]["datasets"] == [{"id": "dataset-1", "name": "知识库"}]
    assert result[0]["query"] == {"mode": "generated_query"}
    assert result[0]["retrieval"]["top_k"] == 3


def test_queued_run_rechecks_revoked_dataset_access(monkeypatch):
    monkeypatch.setattr(knowledge, "available_sets", lambda *args: [{"id": "allowed"}])
    soul = SimpleNamespace(
        knowledge=SimpleNamespace(
            sets=[SimpleNamespace(datasets=[SimpleNamespace(id="revoked")])]
        )
    )
    with pytest.raises(Forbidden):
        knowledge.validate_run_knowledge("tenant", "account", soul)
    soul.knowledge.sets[0].datasets[0].id = "allowed"
    knowledge.validate_run_knowledge("tenant", "account", soul)


@pytest.mark.parametrize("rerank", [True, False])
def test_dataset_retrieval_and_metadata_settings_survive_layer_validation(rerank):
    settings = {
        "search_method": "hybrid_search",
        "top_k": 9,
        "reranking_enable": rerank,
        "reranking_mode": "reranking_model",
        "reranking_model": {
            "reranking_provider_name": "provider",
            "reranking_model_name": "reranker",
        },
        "score_threshold_enabled": True,
        "score_threshold": 0.42,
        "metadata_filtering_conditions": {
            "logical_operator": "and",
            "conditions": [{"name": "year", "comparison_operator": "=", "value": 2026}],
        },
    }
    layer = DifyKnowledgeBaseLayerConfig.model_validate(
        {
            "sets": [
                {
                    "id": "kb",
                    "name": "资料库",
                    "datasets": [{"id": "kb"}],
                    "query": {"mode": "generated_query"},
                    "retrieval": knowledge.dataset_retrieval_config(settings),
                    "metadata_filtering": knowledge.dataset_metadata_filter(settings),
                }
            ]
        }
    )
    policy = layer.sets[0]
    assert policy.retrieval.reranking_enable is rerank
    assert policy.retrieval.reranking_model is not None
    assert policy.retrieval.reranking_model.model == "reranker"
    assert policy.retrieval.top_k == 9 and policy.retrieval.score_threshold == 0.42
    assert policy.metadata_filtering.mode == "manual"
    assert policy.metadata_filtering.conditions is not None
    assert policy.metadata_filtering.conditions.conditions[0].value == 2026


def test_disabled_threshold_and_weighted_reranking_are_not_replaced():
    weights = {
        "vector_setting": {"vector_weight": 0.7},
        "keyword_setting": {"keyword_weight": 0.3},
    }
    settings = {
        "top_k": 7,
        "reranking_enable": True,
        "reranking_mode": "weighted_score",
        "weights": weights,
        "score_threshold_enabled": False,
        "score_threshold": 0.99,
    }
    policy = knowledge.dataset_retrieval_config(settings)
    assert policy["score_threshold"] == 0.0
    assert policy["reranking_mode"] == "weighted_score"
    assert policy["weights"] == weights
    assert policy["weights"] is not weights
