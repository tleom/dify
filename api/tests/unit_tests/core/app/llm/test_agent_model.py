"""Use real scoped SQL for referenced model credentials; provider I/O is replaced."""

import json
from types import SimpleNamespace
from typing import Literal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from dify_agent.layers.dify_plugin.configs import DifyModelCredentialRef
from sqlalchemy.orm import Session

from core.app.llm import agent_model
from core.entities.provider_configuration import ProviderConfiguration, ProviderModelBundle
from core.entities.provider_entities import CustomConfiguration, ModelSettings
from graphon.model_runtime.entities.model_entities import ModelType
from models.account import Account
from models.enums import PermissionEnum
from models.provider import ProviderCredential, ProviderModelCredential, ProviderType

type CredentialContext = tuple[Session, ProviderConfiguration, str, str, MagicMock]


@pytest.fixture
def context(sqlite_session: Session, monkeypatch: pytest.MonkeyPatch) -> CredentialContext:
    tenant, user = str(uuid4()), str(uuid4())
    account = Account(name="caller", email="caller@example.test")
    account.id = user
    sqlite_session.add(account)
    configuration = ProviderConfiguration.model_construct(
        tenant_id=tenant,
        provider=SimpleNamespace(
            provider="langgenius/demo/demo",
            provider_credential_schema=SimpleNamespace(credential_form_schemas=[]),
            model_credential_schema=SimpleNamespace(credential_form_schemas=[]),
        ),
        using_provider_type=ProviderType.CUSTOM,
        custom_configuration=CustomConfiguration(),
        model_settings=[ModelSettings(model="test-model", model_type=ModelType.LLM)],
    )
    monkeypatch.setattr(ProviderConfiguration, "_get_provider_names", lambda _self: ["langgenius/demo/demo", "demo"])
    monkeypatch.setattr(ProviderConfiguration, "extract_secret_variables", lambda _self, _forms: ["api_key"])
    monkeypatch.setattr(agent_model, "runtime_check_credential_policy_compliance", lambda **_kwargs: None)
    decrypt = MagicMock(return_value="decrypted-test-secret")
    monkeypatch.setattr(agent_model.encrypter, "decrypt_token", decrypt)
    return sqlite_session, configuration, tenant, user, decrypt


def add_credential(
    context: CredentialContext, kind: Literal["provider", "model"] = "provider", **changes: object
) -> DifyModelCredentialRef:
    session, _, tenant, user, _ = context
    encrypted = json.dumps({"api_key": "encrypted-test-value", "endpoint": "test"})
    if kind == "model":
        record = ProviderModelCredential(
            tenant_id=tenant, provider_name="demo", credential_name="explicit", encrypted_config=encrypted,
            model_name="test-model", model_type=ModelType.LLM,
        )
    else:
        record = ProviderCredential(
            tenant_id=tenant, provider_name="demo", credential_name="explicit", encrypted_config=encrypted,
            user_id=user, visibility=PermissionEnum.ALL_TEAM,
        )
    for key, value in changes.items():
        setattr(record, key, value)
    session.add(record)
    session.commit()
    return DifyModelCredentialRef(type=kind, id=record.id)


@pytest.mark.parametrize("kind", ["provider", "model"])
def test_reference_uses_scoped_saved_credential(context: CredentialContext, kind: Literal["provider", "model"]) -> None:
    _, configuration, tenant, user, decrypt = context
    reference = add_credential(context, kind)
    values = agent_model._resolve_credentials(configuration, tenant, "test-model", reference)
    assert values == {"api_key": "decrypted-test-secret", "endpoint": "test"}
    decrypt.assert_called_once_with(tenant_id=tenant, token="encrypted-test-value")


@pytest.mark.parametrize(
    ("kind", "changes"),
    [
        ("provider", {"tenant_id": str(uuid4())}),
        ("provider", {"provider_name": "other-provider"}),
        ("model", {"model_name": "different-model"}),
        ("model", {"model_type": ModelType.TEXT_EMBEDDING}),
        ("model", {"tenant_id": str(uuid4())}),
    ],
)
def test_reference_rejects_wrong_tenant_provider_or_model(
    context: CredentialContext, kind: Literal["provider", "model"], changes: dict[str, object]
) -> None:
    _, configuration, tenant, user, decrypt = context
    reference = add_credential(context, kind, **changes)
    with pytest.raises(ValueError, match="unavailable or not authorized"):
        agent_model._resolve_credentials(configuration, tenant, "test-model", reference)
    decrypt.assert_not_called()


@pytest.mark.parametrize("visibility", [PermissionEnum.ONLY_ME, PermissionEnum.PARTIAL_TEAM])
@pytest.mark.parametrize("caller_kind", ["end-user", "other-account"])
def test_saved_reference_remains_executable_for_published_callers(
    context: CredentialContext, monkeypatch: pytest.MonkeyPatch, visibility: PermissionEnum, caller_kind: str
) -> None:
    _, configuration, tenant, user, _ = context
    reference = add_credential(context, user_id=str(uuid4()), visibility=visibility)
    monkeypatch.setattr(ProviderConfiguration, "get_provider_model", lambda _self, **_kwargs: MagicMock())
    bundle = ProviderModelBundle.model_construct(
        configuration=configuration, model_type_instance=MagicMock(model_type=ModelType.LLM)
    )
    manager = MagicMock()
    manager.get_provider_model_bundle.return_value = bundle
    instance = agent_model.resolve_referenced_agent_model(
        provider_manager=manager,
        tenant_id=tenant,
        user_id=str(uuid4()) if caller_kind == "end-user" else user,
        provider="langgenius/demo/demo",
        model="test-model",
        credential_ref=reference,
    )
    assert instance.credentials["endpoint"] == "test"


def test_saved_private_reference_still_enforces_credential_policy(
    context: CredentialContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, configuration, tenant, _, decrypt = context
    reference = add_credential(context, user_id=str(uuid4()), visibility=PermissionEnum.ONLY_ME)
    policy = MagicMock(side_effect=ValueError("credential policy denied"))
    monkeypatch.setattr(agent_model, "runtime_check_credential_policy_compliance", policy)
    with pytest.raises(ValueError, match="credential policy denied"):
        agent_model._resolve_credentials(configuration, tenant, "test-model", reference)
    decrypt.assert_not_called()


def test_deleted_or_undecodable_reference_never_falls_back(context: CredentialContext) -> None:
    session, configuration, tenant, user, decrypt = context
    reference = add_credential(context)
    decrypt.side_effect = ValueError("invalid encrypted token")
    with pytest.raises(ValueError, match="could not be decoded"):
        agent_model._resolve_credentials(configuration, tenant, "test-model", reference)
    record = session.get(ProviderCredential, reference.id)
    assert record is not None
    session.delete(record)
    session.commit()
    with pytest.raises(ValueError, match="unavailable or not authorized"):
        agent_model._resolve_credentials(configuration, tenant, "test-model", reference)


def test_pinned_model_uses_custom_billing_and_disables_load_balancing_without_mutation(
    context: CredentialContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, configuration, tenant, user, _ = context
    reference = add_credential(context)
    configuration.using_provider_type = ProviderType.SYSTEM
    configuration.model_settings[0].load_balancing_enabled = True
    configuration.model_settings[0].load_balancing_configs = [MagicMock()]
    provider_model = MagicMock()
    monkeypatch.setattr(ProviderConfiguration, "get_provider_model", lambda _self, **_kwargs: provider_model)
    bundle = ProviderModelBundle.model_construct(
        configuration=configuration, model_type_instance=MagicMock(model_type=ModelType.LLM)
    )
    manager = MagicMock()
    manager.get_provider_model_bundle.return_value = bundle
    instance = agent_model.resolve_referenced_agent_model(
        provider_manager=manager,
        tenant_id=tenant,
        user_id=user,
        provider="langgenius/demo/demo",
        model="test-model",
        credential_ref=reference,
    )
    assert instance.credentials["api_key"] == "decrypted-test-secret"
    assert instance.load_balancing_manager is None
    assert instance.provider_model_bundle.configuration.using_provider_type == ProviderType.CUSTOM
    assert configuration.using_provider_type == ProviderType.SYSTEM
    assert len(configuration.model_settings[0].load_balancing_configs) == 1
    assert configuration.custom_configuration.provider is None
    provider_model.raise_for_status.assert_called_once()
    with pytest.raises(ValueError, match="does not match"):
        agent_model.resolve_referenced_agent_model(
            provider_manager=manager,
            tenant_id=tenant,
            user_id=user,
            provider="langgenius/demo/demo",
            model="test-model",
            credential_ref=reference.model_copy(update={"provider": "different"}),
        )
