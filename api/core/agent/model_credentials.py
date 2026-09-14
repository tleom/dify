"""Authorize model credential selection before an Agent configuration is saved."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.entities.provider_configuration import _model_type_db_literals
from core.helper.credential_visibility import apply_credential_visibility_filter
from graphon.model_runtime.entities.model_entities import ModelType
from models.account import Account
from models.agent_config_entities import AgentSoulModelConfig
from models.credential_permission import CredentialType
from models.provider import ProviderCredential, ProviderModelCredential
from models.provider_ids import ModelProviderID


def validate_model_credential_selection(
    *,
    session: Session,
    tenant_id: str,
    account_id: str,
    model: AgentSoulModelConfig | None,
    previous_model: AgentSoulModelConfig | None = None,
) -> None:
    """Authorize new references; a prior model must come from this Agent's DB row.

    Runtime invocations retain the published credential independently of the
    caller's selection permissions. Reusing another Agent's reference is a new
    selection, even if its ID was obtained from a readable composer payload.
    """
    if model is None or model.credential_ref is None:
        return
    reference = model.credential_ref
    if (
        previous_model is not None
        and model.model_provider == previous_model.model_provider
        and model.model == previous_model.model
        and reference == previous_model.credential_ref
    ):
        return
    error = "Model credential reference is unavailable or not authorized for selection"
    if reference.type not in {"provider", "model"} or not reference.id:
        raise ValueError(error)
    provider = ModelProviderID(model.model_provider)
    names = {model.model_provider, str(provider)}
    if provider.is_langgenius():
        names.add(provider.provider_name)
    if reference.provider and reference.provider not in names:
        raise ValueError(error)
    account = session.get(Account, account_id)
    if account is None:
        raise ValueError(error)
    if reference.type == "provider":
        statement = select(ProviderCredential.id).where(
            ProviderCredential.id == reference.id,
            ProviderCredential.tenant_id == tenant_id,
            ProviderCredential.provider_name.in_(names),
        )
        statement = apply_credential_visibility_filter(
            statement,
            model_id_column=ProviderCredential.id,
            model_user_id_column=ProviderCredential.user_id,
            model_visibility_column=ProviderCredential.visibility,
            credential_type=CredentialType.PROVIDER_CREDENTIAL,
            user=account,
        )
    else:
        statement = select(ProviderModelCredential.id).where(
            ProviderModelCredential.id == reference.id,
            ProviderModelCredential.tenant_id == tenant_id,
            ProviderModelCredential.provider_name.in_(names),
            ProviderModelCredential.model_name == model.model,
            ProviderModelCredential.model_type.in_(_model_type_db_literals(ModelType.LLM)),
        )
    if session.scalar(statement) is None:
        raise ValueError(error)
