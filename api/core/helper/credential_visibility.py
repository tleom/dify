"""Shared SQL predicate for credential visibility in services and runtimes."""

from sqlalchemy import or_, select
from sqlalchemy.orm import InstrumentedAttribute

from models.account import Account
from models.credential_permission import CredentialPermission
from models.enums import PermissionEnum


def apply_credential_visibility_filter(
    query,
    *,
    model_id_column: InstrumentedAttribute,
    model_user_id_column: InstrumentedAttribute,
    model_visibility_column: InstrumentedAttribute,
    credential_type: str,
    user: Account,
):
    """Allow team credentials, legacy rows, the creator and explicit members.

    The caller must scope the query to the tenant and credential type. Personal
    credentials have no administrator bypass.
    """
    partial_subquery = (
        select(CredentialPermission.credential_id)
        .where(
            CredentialPermission.credential_type == credential_type,
            CredentialPermission.account_id == user.id,
        )
        .correlate_except(CredentialPermission)
    )
    return query.where(
        or_(
            model_visibility_column == PermissionEnum.ALL_TEAM,
            model_user_id_column.is_(None),
            model_user_id_column == user.id,
            model_id_column.in_(partial_subquery),
        )
    )
