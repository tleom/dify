"""Account-visible Dify datasets exposed as searchable workbench knowledge sets."""

import copy

from werkzeug.exceptions import Forbidden

from configs import dify_config
from core.db.session_factory import session_factory
from models import Account
from services.dataset_service import DatasetService
from services.workbench.authorization import can_retrieve_dataset


def dataset_retrieval_config(settings: dict) -> dict:
    """Translate the dataset's saved retrieval policy without replacing it."""
    reranking = settings.get("reranking_model") or {}
    provider = reranking.get("reranking_provider_name")
    model = reranking.get("reranking_model_name")
    return {
        "mode": "multiple",
        "top_k": settings.get("top_k", 4),
        "reranking_enable": settings.get("reranking_enable", False),
        "reranking_mode": settings.get("reranking_mode") or "reranking_model",
        "reranking_model": {"provider": provider, "model": model} if provider and model else None,
        "weights": copy.deepcopy(settings.get("weights")),
        "score_threshold": (settings.get("score_threshold") or 0.0) if settings.get("score_threshold_enabled") else 0.0,
    }


def dataset_metadata_filter(settings: dict) -> dict:
    """Use the saved dataset metadata conditions for both search and document reads."""
    conditions = copy.deepcopy(settings.get("metadata_filtering_conditions"))
    if not conditions or not conditions.get("conditions"):
        return {"mode": "disabled"}
    conditions["logical_operator"] = conditions.get("logical_operator") or "and"
    return {"mode": "manual", "conditions": conditions}


def available_sets(tenant_id: str, account_id: str) -> list[dict]:
    from services.enterprise.rbac_service import RBACService

    with session_factory.create_session() as session:
        account = session.get(Account, account_id)
        if account is None:
            raise Forbidden()
        account.set_tenant_id_with_session(tenant_id, session=session)
        if account.current_tenant_id != tenant_id:
            raise Forbidden()
        accessible_ids = None
        if dify_config.RBAC_ENABLED:
            scope = RBACService.DatasetAccess.whitelist_resources(tenant_id, account_id)
            if not scope.unrestricted:
                accessible_ids = list(scope.resource_ids)
        result: list[dict] = []
        page = 1
        while True:
            datasets, total = DatasetService.get_datasets(
                page,
                100,
                session,
                tenant_id,
                account,
                accessible_dataset_ids=accessible_ids,
            )
            for dataset in datasets:
                if not can_retrieve_dataset(tenant_id, account_id, dataset.id):
                    continue
                settings = dataset.retrieval_model or {}
                result.append(
                    {
                        "id": dataset.id,
                        "name": dataset.name,
                        "description": dataset.description,
                        "datasets": [{"id": dataset.id, "name": dataset.name}],
                        "query": {"mode": "generated_query"},
                        "retrieval": dataset_retrieval_config(settings),
                        "metadata_filtering": dataset_metadata_filter(settings),
                    }
                )
            if page * 100 >= total:
                names = [item["name"].strip().casefold() for item in result]
                for item in result:
                    if names.count(item["name"].strip().casefold()) > 1:
                        item["name"] = f"{item['name']} ({item['id'][:8]})"
                return result
            page += 1


def validate_run_knowledge(tenant_id: str, account_id: str, soul) -> None:
    if not soul.knowledge.sets:
        return
    allowed = {item["id"] for item in available_sets(tenant_id, account_id)}
    requested = {dataset.id for item in soul.knowledge.sets for dataset in item.datasets}
    if not requested.issubset(allowed):
        raise Forbidden("所选知识库已删除或访问权限已变更，请重新选择")
