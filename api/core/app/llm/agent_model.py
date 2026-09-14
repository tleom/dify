"""Resolve explicit Agent model credentials inside the tenant-owned API runtime."""

import json
from typing import Any

from dify_agent.layers.dify_plugin.configs import DifyModelCredentialRef
from sqlalchemy import select

from core.db.session_factory import session_factory
from core.entities import PluginCredentialType
from core.entities.provider_configuration import _model_type_db_literals
from core.entities.provider_entities import CustomModelConfiguration, CustomProviderConfiguration
from core.helper import encrypter
from core.helper.credential_utils import runtime_check_credential_policy_compliance
from core.model_manager import ModelInstance
from core.provider_manager import ProviderManager
from graphon.model_runtime.entities.model_entities import ModelType
from models.provider import ProviderCredential, ProviderModelCredential, ProviderType


def resolve_referenced_agent_model(
    *,
    provider_manager: ProviderManager,
    tenant_id: str,
    user_id: str | None,
    provider: str,
    model: str,
    credential_ref: DifyModelCredentialRef,
) -> ModelInstance:
    """Pin this invocation to a validated reference, with no default/LB fallback.

    The API runtime supplies this reference from the saved Agent configuration;
    it is not a credential-selection endpoint. A published app keeps using its
    configured credential even when it is hidden from the invoking EndUser or
    another workspace member. Tenant ownership, credential policy and exact
    provider/model identity are still checked on each invocation. The provider
    configuration is copied so unrelated calls are never mutated.
    """
    bundle = provider_manager.get_provider_model_bundle(
        tenant_id=tenant_id, provider=provider, model_type=ModelType.LLM
    )
    configuration = bundle.configuration
    names = configuration._get_provider_names()
    if credential_ref.provider and credential_ref.provider not in names:
        raise ValueError("Model credential reference does not match the selected provider")
    credentials = _resolve_credentials(configuration, tenant_id, model, credential_ref)
    custom = configuration.custom_configuration.model_copy(deep=True)
    if credential_ref.type == "provider":
        custom.provider = CustomProviderConfiguration(credentials=credentials, current_credential_id=credential_ref.id)
    selected = next((item for item in custom.models if item.model == model and item.model_type == ModelType.LLM), None)
    if selected is not None:
        selected.credentials = credentials
        selected.current_credential_id = credential_ref.id
    elif credential_ref.type == "model":
        custom.models.append(
            CustomModelConfiguration(
                model=model,
                model_type=ModelType.LLM,
                credentials=credentials,
                current_credential_id=credential_ref.id,
            )
        )
    configuration = configuration.model_copy(
        update={
            "using_provider_type": ProviderType.CUSTOM,
            "custom_configuration": custom,
            "model_settings": [
                item.model_copy(update={"load_balancing_configs": [], "load_balancing_enabled": False})
                if item.model == model and item.model_type == ModelType.LLM
                else item
                for item in configuration.model_settings
            ],
        }
    )
    provider_model = configuration.get_provider_model(model_type=ModelType.LLM, model=model)
    if provider_model is None:
        raise ValueError("Referenced Agent model is unavailable")
    provider_model.raise_for_status()
    return ModelInstance(bundle.model_copy(update={"configuration": configuration}), model, credentials=credentials)


def _resolve_credentials(configuration, tenant_id, model, reference) -> dict[str, Any]:
    record_type: type[ProviderCredential] | type[ProviderModelCredential] = (
        ProviderCredential if reference.type == "provider" else ProviderModelCredential
    )
    statement = select(record_type.encrypted_config).where(
        record_type.id == reference.id,
        record_type.tenant_id == tenant_id,
        record_type.provider_name.in_(configuration._get_provider_names()),
    )
    if reference.type == "model":
        statement = statement.where(
            ProviderModelCredential.model_name == model,
            ProviderModelCredential.model_type.in_(_model_type_db_literals(ModelType.LLM)),
        )
    with session_factory.create_session() as session:
        encrypted = session.scalar(statement)
        if not encrypted:
            raise ValueError("Referenced model credential is unavailable or not authorized")
    runtime_check_credential_policy_compliance(
        credential_id=reference.id,
        provider=configuration.provider.provider,
        credential_type=PluginCredentialType.MODEL,
    )
    try:
        values = json.loads(encrypted)
        if not isinstance(values, dict) or not values:
            raise ValueError()
        schema = (
            configuration.provider.provider_credential_schema
            if reference.type == "provider"
            else configuration.provider.model_credential_schema
        )
        if schema is None:
            raise ValueError()
        for key in configuration.extract_secret_variables(schema.credential_form_schemas):
            if values.get(key) is not None:
                values[key] = encrypter.decrypt_token(tenant_id=tenant_id, token=values[key])
        return values
    except Exception as exc:
        raise ValueError("Referenced model credential could not be decoded") from exc
