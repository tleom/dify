"""Exercise credential selection policy and Agent persistence with real scoped SQL."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.agent.model_credentials import validate_model_credential_selection
from graphon.model_runtime.entities.model_entities import ModelType
from models.account import Account
from models.agent import (
    Agent,
    AgentConfigDraftType,
    AgentConfigRevision,
    AgentConfigRevisionOperation,
    AgentConfigSnapshot,
    AgentScope,
    AgentSource,
    WorkflowAgentNodeBinding,
)
from models.agent_config_entities import AgentSoulConfig, AgentSoulModelConfig, AgentSoulModelCredentialRef
from models.credential_permission import CredentialPermission, CredentialType
from models.enums import PermissionEnum
from models.provider import ProviderCredential, ProviderModelCredential
from services.agent.composer_service import AgentComposerService
from services.agent.composer_validator import ComposerConfigValidator
from services.agent.errors import InvalidComposerConfigError
from services.agent.roster_service import AgentRosterService
from services.entities.agent_entities import (
    ComposerSavePayload,
    ComposerSaveStrategy,
    ComposerVariant,
    RosterAgentCreatePayload,
)


@pytest.fixture
def selection(sqlite_session: Session) -> tuple[Session, str, str, AgentSoulModelConfig]:
    account = Account(name="editor", email="editor@example.test")
    sqlite_session.add(account)
    sqlite_session.flush()
    tenant_id = str(uuid4())
    credential = ProviderCredential(
        tenant_id=tenant_id,
        provider_name="demo",
        credential_name="private",
        encrypted_config="encrypted-placeholder",
        user_id=str(uuid4()),
        visibility=PermissionEnum.ONLY_ME,
    )
    sqlite_session.add(credential)
    sqlite_session.commit()
    model = AgentSoulModelConfig(
        plugin_id="langgenius/demo",
        model_provider="langgenius/demo/demo",
        model="test-model",
        credential_ref=AgentSoulModelCredentialRef(type="provider", id=credential.id),
    )
    return sqlite_session, tenant_id, account.id, model


@pytest.mark.parametrize("case", ["private", "owner", "team", "partial-denied", "partial-granted", "legacy"])
def test_selection_enforces_account_visibility(
    selection: tuple[Session, str, str, AgentSoulModelConfig], case: str
) -> None:
    session, tenant_id, account_id, model = selection
    assert model.credential_ref is not None
    credential = session.get(ProviderCredential, model.credential_ref.id)
    assert credential is not None
    if case == "owner":
        credential.user_id = account_id
    elif case == "team":
        credential.visibility = PermissionEnum.ALL_TEAM
    elif case.startswith("partial"):
        credential.visibility = PermissionEnum.PARTIAL_TEAM
        if case == "partial-granted":
            session.add(
                CredentialPermission(
                    tenant_id=tenant_id,
                    credential_id=credential.id,
                    credential_type=CredentialType.PROVIDER_CREDENTIAL,
                    account_id=account_id,
                )
            )
    elif case == "legacy":
        credential.user_id = None
    session.flush()
    if case in {"private", "partial-denied"}:
        with pytest.raises(ValueError, match="not authorized"):
            validate_model_credential_selection(
                session=session, tenant_id=tenant_id, account_id=account_id, model=model
            )
    else:
        validate_model_credential_selection(session=session, tenant_id=tenant_id, account_id=account_id, model=model)


@pytest.mark.parametrize("mismatch", ["tenant", "provider", "reference-provider", "model", "model-type"])
def test_model_reference_requires_the_selected_tenant_provider_and_model(
    selection: tuple[Session, str, str, AgentSoulModelConfig], mismatch: str
) -> None:
    session, tenant_id, account_id, model = selection
    credential = ProviderModelCredential(
        tenant_id=str(uuid4()) if mismatch == "tenant" else tenant_id,
        provider_name="other" if mismatch == "provider" else "demo",
        model_name="other-model" if mismatch == "model" else model.model,
        model_type=ModelType.TEXT_EMBEDDING if mismatch == "model-type" else ModelType.LLM,
        credential_name="model credential",
        encrypted_config="encrypted-placeholder",
    )
    session.add(credential)
    session.flush()
    model.credential_ref = AgentSoulModelCredentialRef(
        type="model", id=credential.id, provider="other" if mismatch == "reference-provider" else None
    )
    with pytest.raises(ValueError, match="not authorized"):
        validate_model_credential_selection(session=session, tenant_id=tenant_id, account_id=account_id, model=model)


def test_unchanged_reference_can_be_preserved_but_identity_changes_need_selection_rights(
    selection: tuple[Session, str, str, AgentSoulModelConfig],
) -> None:
    session, tenant_id, account_id, model = selection
    updated = model.model_copy(deep=True)
    updated.model_settings.temperature = 0.2
    validate_model_credential_selection(
        session=session, tenant_id=tenant_id, account_id=account_id, model=updated, previous_model=model
    )
    updated.model = "new-model"
    with pytest.raises(ValueError, match="not authorized"):
        validate_model_credential_selection(
            session=session, tenant_id=tenant_id, account_id=account_id, model=updated, previous_model=model
        )


@pytest.mark.parametrize("destination", ["draft", "snapshot", "roster", "backing-app"])
def test_persistence_rejects_a_private_reference_before_saving_it(
    selection: tuple[Session, str, str, AgentSoulModelConfig], destination: str
) -> None:
    session, tenant_id, account_id, model = selection
    agent = Agent(
        tenant_id=tenant_id,
        name="target",
        created_by=account_id,
        updated_by=account_id,
        scope=AgentScope.ROSTER,
        source=AgentSource.ROSTER,
    )
    session.add(agent)
    session.flush()
    initial = AgentConfigSnapshot(
        tenant_id=tenant_id, agent_id=agent.id, version=1, config_snapshot=AgentSoulConfig(), created_by=account_id
    )
    session.add(initial)
    session.flush()
    agent.active_config_snapshot_id = initial.id
    soul = AgentSoulConfig(model=model)
    if destination == "draft":
        with pytest.raises(InvalidComposerConfigError, match="not authorized"):
            AgentComposerService._save_agent_draft(
                session=session,
                tenant_id=tenant_id,
                agent=agent,
                draft_type=AgentConfigDraftType.DRAFT,
                account_id=None,
                agent_soul=soul,
                account_id_for_audit=account_id,
            )
    elif destination == "snapshot":
        with pytest.raises(InvalidComposerConfigError, match="not authorized"):
            AgentComposerService._create_config_version(
                session=session,
                tenant_id=tenant_id,
                agent_id=agent.id,
                account_id=account_id,
                agent_soul=soul,
                operation=AgentConfigRevisionOperation.CREATE_VERSION,
                version_note=None,
                home_snapshot_id=None,
            )
    elif destination == "roster":
        with pytest.raises(InvalidComposerConfigError, match="not authorized"):
            AgentRosterService(session)._create_roster_agent_in_transaction(
                tenant_id=tenant_id,
                account_id=account_id,
                payload=RosterAgentCreatePayload(name="new agent", agent_soul=soul),
                source=AgentSource.ROSTER,
            )
    else:
        with pytest.raises(InvalidComposerConfigError, match="not authorized"):
            AgentRosterService(session).create_backing_agent_for_app(
                tenant_id=tenant_id, account_id=account_id, app_id=str(uuid4()), name="new app", initial_soul=soul
            )


@pytest.mark.parametrize("same_agent", [True, False])
def test_snapshot_trust_requires_the_target_agent_owner_chain(
    selection: tuple[Session, str, str, AgentSoulModelConfig], same_agent: bool
) -> None:
    session, tenant_id, account_id, model = selection
    source = Agent(
        tenant_id=tenant_id,
        name="source",
        created_by=account_id,
        updated_by=account_id,
        scope=AgentScope.ROSTER,
        source=AgentSource.ROSTER,
    )
    target = Agent(
        tenant_id=tenant_id,
        name="target",
        created_by=account_id,
        updated_by=account_id,
        scope=AgentScope.ROSTER,
        source=AgentSource.ROSTER,
    )
    session.add_all([source, target])
    session.flush()
    soul = AgentSoulConfig(model=model)
    previous = AgentConfigSnapshot(
        tenant_id=tenant_id,
        agent_id=target.id if same_agent else source.id,
        version=1,
        config_snapshot=soul,
        created_by=account_id,
    )
    session.add(previous)
    session.flush()
    if same_agent:
        saved = AgentComposerService._create_config_version(
            session=session,
            tenant_id=tenant_id,
            agent_id=target.id,
            account_id=account_id,
            agent_soul=soul,
            operation=AgentConfigRevisionOperation.CREATE_VERSION,
            version_note=None,
            home_snapshot_id=None,
            previous_snapshot_id=previous.id,
        )
        assert saved.config_snapshot.model is not None
        assert saved.config_snapshot.model.credential_ref == model.credential_ref
    else:
        with pytest.raises(InvalidComposerConfigError, match="not authorized"):
            AgentComposerService._create_config_version(
                session=session,
                tenant_id=tenant_id,
                agent_id=target.id,
                account_id=account_id,
                agent_soul=soul,
                operation=AgentConfigRevisionOperation.CREATE_VERSION,
                version_note=None,
                home_snapshot_id=None,
                previous_snapshot_id=previous.id,
            )


@pytest.mark.parametrize("change_model", [False, True])
def test_workflow_new_version_preserves_only_the_current_snapshots_credential(
    selection: tuple[Session, str, str, AgentSoulModelConfig], change_model: bool
) -> None:
    session, tenant_id, account_id, model = selection
    agent = Agent(
        tenant_id=tenant_id,
        name="workflow agent",
        created_by=account_id,
        updated_by=account_id,
        scope=AgentScope.WORKFLOW_ONLY,
        source=AgentSource.WORKFLOW,
    )
    session.add(agent)
    session.flush()
    soul = AgentSoulConfig(model=model)
    previous = AgentConfigSnapshot(
        tenant_id=tenant_id, agent_id=agent.id, version=1, config_snapshot=soul, created_by=account_id
    )
    session.add(previous)
    session.flush()
    agent.active_config_snapshot_id = previous.id
    binding = WorkflowAgentNodeBinding(agent_id=agent.id, current_snapshot_id=previous.id)
    updated = soul.model_copy(deep=True)
    assert updated.model is not None
    updated.model.model_settings.temperature = 0.2
    if change_model:
        updated.model.model = "another-model"
    payload = ComposerSavePayload(
        variant=ComposerVariant.WORKFLOW,
        save_strategy=ComposerSaveStrategy.SAVE_AS_NEW_VERSION,
        agent_soul=updated,
    )
    if change_model:
        with pytest.raises(InvalidComposerConfigError, match="not authorized"):
            AgentComposerService._save_as_new_version(
                session=session, tenant_id=tenant_id, account_id=account_id, binding=binding, payload=payload
            )
        return
    AgentComposerService._save_as_new_version(
        session=session, tenant_id=tenant_id, account_id=account_id, binding=binding, payload=payload
    )
    saved = session.get(AgentConfigSnapshot, binding.current_snapshot_id)
    assert saved is not None
    assert saved.id != previous.id
    assert saved.config_snapshot.model is not None
    assert saved.config_snapshot.model.credential_ref == model.credential_ref
    assert saved.config_snapshot.model.model_settings.temperature == 0.2
    assert agent.active_config_snapshot_id == saved.id
    revision = session.scalar(select(AgentConfigRevision).where(AgentConfigRevision.current_snapshot_id == saved.id))
    assert revision is not None
    assert revision.previous_snapshot_id == previous.id


@pytest.mark.parametrize(
    "strategy", [ComposerSaveStrategy.SAVE_TO_CURRENT_VERSION, ComposerSaveStrategy.SAVE_AS_NEW_VERSION]
)
def test_composer_reserves_the_activity_tool_name(strategy: ComposerSaveStrategy) -> None:
    soul = AgentSoulConfig.model_validate(
        {
            "tools": {
                "dify_tools": [
                    {
                        "provider_type": "plugin",
                        "plugin_id": "test/tools",
                        "provider": "test",
                        "tool_name": "report_activity",
                        "credential_type": "unauthorized",
                    }
                ]
            }
        }
    )
    payload = ComposerSavePayload(variant=ComposerVariant.AGENT_APP, save_strategy=strategy, agent_soul=soul)
    with pytest.raises(InvalidComposerConfigError, match="reserved"):
        ComposerConfigValidator.validate_draft_save_payload(payload)
