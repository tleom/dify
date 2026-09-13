"""Account-visible Dify datasets exposed as searchable workbench knowledge sets."""

from werkzeug.exceptions import Forbidden

from configs import dify_config
from core.db.session_factory import session_factory
from models import Account
from services.dataset_service import DatasetService
from services.workbench.authorization import can_retrieve_dataset


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
                        "retrieval": {
                            "mode": "multiple",
                            "top_k": settings.get("top_k", 4),
                            "reranking_enable": False,
                            "score_threshold": settings.get("score_threshold")
                            if settings.get("score_threshold_enabled")
                            else None,
                        },
                        "metadata_filtering": {"mode": "disabled"},
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
