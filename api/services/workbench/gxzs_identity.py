"""Provision and resolve workbench-only GXZS identities in existing Dify tables.

An account_integrates row is the durable binding. First access creates one normal
member atomically; later requests never restore a disabled account or membership
removed by an administrator. The member has no password and receives no console
session. Only the configured published workbench template can be run by this grant.
"""

from datetime import UTC, datetime
from uuid import UUID

from redis.exceptions import LockError
from sqlalchemy import select
from werkzeug.exceptions import Forbidden, ServiceUnavailable, Unauthorized

from configs import dify_config
from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from models.account import (
    Account,
    AccountIntegrate,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
    TenantAccountRole,
    TenantStatus,
)
from services.workbench.gxzs_assertion import GxzsAssertion, external_id

PROVIDER = "gxzs"


def consume_assertion(claims: GxzsAssertion) -> None:
    """Consume a nonce before any account mutation; retries need a fresh assertion."""
    nonce = f"workbench:gxzs:nonce:{external_id(claims)}:{claims.jti}"
    if not redis_client.set(nonce, "1", nx=True, ex=120):
        raise Unauthorized("登录凭证已使用，请重试")


def resolve_account(claims: GxzsAssertion) -> Account:
    """Create once under a distributed lock and return an active detached account."""
    mapped_tenant = dify_config.GXZS_WORKBENCH_TENANTS.get(claims.tenant_id)
    try:
        tenant_id = str(UUID(mapped_tenant)) if mapped_tenant else None
    except (ValueError, TypeError, AttributeError) as error:
        raise ServiceUnavailable("工作台租户映射配置无效") from error
    if not tenant_id:
        raise Forbidden("当前租户尚未开通智能问答")
    open_id = external_id(claims)
    try:
        with redis_client.lock(f"workbench:gxzs:account:{open_id}", timeout=15, blocking_timeout=5):
            with session_factory.create_session() as session:
                with session.begin():
                    tenant = session.get(Tenant, tenant_id)
                    if tenant is None or tenant.status != TenantStatus.NORMAL:
                        raise Forbidden("工作台已停用")
                    binding = session.scalar(
                        select(AccountIntegrate).where(
                            AccountIntegrate.provider == PROVIDER, AccountIntegrate.open_id == open_id
                        )
                    )
                    account: Account | None
                    if binding is None:
                        account = Account(
                            name=claims.name,
                            email=f"{open_id}@gxzs.invalid",
                            normalized_email=f"{open_id}@gxzs.invalid",
                            status=AccountStatus.ACTIVE,
                            initialized_at=datetime.now(UTC).replace(tzinfo=None),
                            interface_language="zh-Hans",
                            timezone="Asia/Shanghai",
                        )
                        session.add(account)
                        session.flush()
                        session.add(
                            AccountIntegrate(
                                account_id=account.id, provider=PROVIDER, open_id=open_id, encrypted_token=""
                            )
                        )
                        session.add(
                            TenantAccountJoin(tenant_id=tenant_id, account_id=account.id, role=TenantAccountRole.NORMAL)
                        )
                        session.flush()
                    else:
                        account = session.get(Account, binding.account_id)
                        if account is None or account.status != AccountStatus.ACTIVE:
                            raise Forbidden("工作台账号已停用")
                    account.set_tenant_id_with_session(tenant_id, session=session)
                    if account.current_tenant_id != tenant_id:
                        raise Forbidden("工作台访问权限已移除")
                    account.name = claims.name
                    # Preserve loaded tenant and role after the transaction closes.
                    session.flush()
                    session.expunge_all()
                return account
    except LockError as error:
        raise ServiceUnavailable("账号正在初始化，请稍后重试") from error


def can_run_template(session, tenant_id: str, account_id: str, agent_id: str) -> bool:
    """Narrow entitlement used only inside WorkbenchService.template.

    Console controllers continue to enforce ordinary RBAC. The owner chain,
    active status and WORKBENCH_ALLOWED_ACCOUNTS are checked by authorize first.
    """
    if (
        not dify_config.GXZS_WORKBENCH_ENABLED
        or tenant_id not in dify_config.GXZS_WORKBENCH_TENANTS.values()
        or dify_config.WORKBENCH_AGENT_TEMPLATES.get(tenant_id) != agent_id
    ):
        return False
    return (
        session.scalar(
            select(AccountIntegrate.id).where(
                AccountIntegrate.account_id == account_id, AccountIntegrate.provider == PROVIDER
            )
        )
        is not None
    )
