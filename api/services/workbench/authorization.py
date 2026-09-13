"""Workbench resource permissions without HTTP path or request dependencies."""

from werkzeug.exceptions import Forbidden

from configs import dify_config
from core.rbac import RBACPermission, RBACResourceScope
from services.enterprise.rbac_service import RBACService
from services.rbac_resource_service import RBACResourceService


def require_agent_run(tenant_id: str, account_id: str, agent_id: str) -> None:
    if not RBACService.CheckAccess.check(
        tenant_id,
        account_id,
        scene=RBACPermission.AGENT_TEST_AND_RUN,
        resource_type=RBACResourceScope.AGENT,
        resource_id=agent_id,
    ):
        raise Forbidden()


def can_retrieve_dataset(tenant_id: str, account_id: str, dataset_id: str) -> bool:
    if not dify_config.RBAC_ENABLED:
        return True
    # Match the dataset transport's maintainer exemption before consulting RBAC.
    if RBACResourceService.get_dataset_maintainer(tenant_id, dataset_id) == account_id:
        return True
    return RBACService.CheckAccess.check(
        tenant_id,
        account_id,
        scene=RBACPermission.DATASET_RETRIEVAL_RECALL,
        resource_type=RBACResourceScope.DATASET,
        resource_id=dataset_id,
    )
