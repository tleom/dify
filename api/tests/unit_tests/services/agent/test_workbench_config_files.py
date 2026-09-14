"""Published reference files survive workbench selection and retain scoped access."""

import json
from dataclasses import dataclass
from datetime import datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from extensions.storage.storage_type import StorageType
from models.agent import Agent, AgentConfigSnapshot, AgentConfigVersionKind, AgentScope, AgentSource
from models.agent_config_entities import AgentConfigFileRefConfig, AgentSoulConfig, AgentSoulModelConfig
from models.enums import CreatorUserRole
from models.model import UploadFile
from models.workbench import WorkbenchChat, WorkbenchRun
from services.agent_config_service import AgentConfigService, AgentConfigServiceError, ConfigDownloadRequest
from services.workbench.policy import Selection, compile_selection
from tests.unit_tests.config_override import apply_config_overrides

TENANT = "11111111-1111-1111-1111-111111111111"
ACCOUNT = "22222222-2222-2222-2222-222222222222"
AGENT = "33333333-3333-3333-3333-333333333333"
SNAPSHOT = "44444444-4444-4444-4444-444444444444"
OTHER = "55555555-5555-5555-5555-555555555555"


@dataclass
class FileRun:
    service: AgentConfigService
    snapshot: AgentConfigSnapshot
    chat: WorkbenchChat
    run: WorkbenchRun
    source: UploadFile


@pytest.fixture
def file_run(sqlite_session: Session, monkeypatch: pytest.MonkeyPatch) -> FileRun:
    apply_config_overrides(monkeypatch, WORKBENCH_ENABLED=True)
    source = UploadFile(
        tenant_id=TENANT,
        storage_type=StorageType.LOCAL,
        key="uploads/guide.md",
        name="操作指南.md",
        size=12,
        extension="md",
        mime_type="text/markdown",
        created_by=OTHER,
        created_by_role=CreatorUserRole.ACCOUNT,
        created_at=datetime(2026, 1, 1),
        used=True,
    )
    soul = AgentSoulConfig(
        model=AgentSoulModelConfig(plugin_id="test/provider", model_provider="test", model="test"),
        config_files=[
            AgentConfigFileRefConfig(name=source.name, file_kind="upload_file", file_id=source.id, size=source.size)
        ],
    )
    assert soul.model is not None
    effective = compile_selection(
        soul.model_dump(mode="json"), Selection(model="test"), {"test": soul.model.model_dump(mode="json")}
    )
    snapshot = AgentConfigSnapshot(id=SNAPSHOT, tenant_id=TENANT, agent_id=AGENT, version=1, config_snapshot=soul)
    chat = WorkbenchChat(
        id="66666666-6666-6666-6666-666666666666",
        tenant_id=TENANT,
        account_id=ACCOUNT,
        agent_id=AGENT,
        app_id=OTHER,
        base_snapshot_id=SNAPSHOT,
    )
    run = WorkbenchRun(
        tenant_id=TENANT,
        account_id=ACCOUNT,
        chat_id=chat.id,
        revision_id=OTHER,
        request_key="config-file-test",
        payload=json.dumps({"effective_soul": effective}),
        status="running",
    )
    sqlite_session.add_all(
        [
            Agent(id=AGENT, tenant_id=TENANT, name="Test", scope=AgentScope.ROSTER, source=AgentSource.ROSTER),
            snapshot,
            chat,
            run,
            source,
        ]
    )
    sqlite_session.commit()
    service = AgentConfigService(
        session_factory=sessionmaker(bind=sqlite_session.get_bind(), expire_on_commit=False), workbench_run_id=run.id
    )
    return FileRun(service=service, snapshot=snapshot, chat=chat, run=run, source=source)


def _download(service: AgentConfigService, name: str = "操作指南.md") -> ConfigDownloadRequest:
    return service.request_download(
        tenant_id=TENANT,
        agent_id=AGENT,
        config_version_id=SNAPSHOT,
        config_version_kind=AgentConfigVersionKind.SNAPSHOT,
        kind="file",
        name=name,
        user_id=ACCOUNT,
    )


def test_published_file_is_listed_and_downloadable_in_workbench(file_run: FileRun) -> None:
    manifest = file_run.service.manifest(
        tenant_id=TENANT,
        agent_id=AGENT,
        config_version_id=SNAPSHOT,
        config_version_kind=AgentConfigVersionKind.SNAPSHOT,
        user_id=ACCOUNT,
    )
    assert manifest["files"] == {
        "items": [
            {
                "id": "操作指南.md",
                "name": "操作指南.md",
                "file_id": file_run.source.id,
                "is_missing": False,
                "size": 12,
                "hash": None,
                "mime_type": None,
            }
        ]
    }
    assert manifest["config_version"] == {"id": SNAPSHOT, "kind": "snapshot", "writable": False}
    download = _download(file_run.service)
    assert (download.filename, download.size, download.mime_type) == ("操作指南.md", 12, "text/markdown")
    assert download.download_uri.startswith(f"/files/{file_run.source.id}/file-preview?")
    assert "as_attachment=true" in download.download_uri


def test_running_task_keeps_its_frozen_file_references(file_run: FileRun, sqlite_session: Session) -> None:
    file_run.snapshot.config_snapshot = AgentSoulConfig()
    sqlite_session.commit()
    assert _download(file_run.service).filename == "操作指南.md"


@pytest.mark.parametrize("boundary", ["tenant", "account", "agent", "snapshot", "deleted_chat", "finished_run"])
def test_workbench_file_download_requires_matching_active_run(
    file_run: FileRun, sqlite_session: Session, boundary: str
) -> None:
    if boundary == "tenant":
        file_run.chat.tenant_id = OTHER
    elif boundary == "account":
        file_run.chat.account_id = OTHER
    elif boundary == "agent":
        file_run.chat.agent_id = OTHER
    elif boundary == "snapshot":
        file_run.chat.base_snapshot_id = OTHER
    elif boundary == "deleted_chat":
        file_run.chat.deleted = 1
    else:
        file_run.run.status = "completed"
    sqlite_session.commit()

    with pytest.raises(AgentConfigServiceError) as error:
        _download(file_run.service)
    assert (error.value.code, error.value.status_code) == ("config_access_denied", 403)


def test_workbench_cannot_download_unpublished_file(file_run: FileRun) -> None:
    with pytest.raises(AgentConfigServiceError) as error:
        _download(file_run.service, "未发布的文件.md")
    assert (error.value.code, error.value.status_code) == ("config_file_not_found", 404)


def test_workbench_cannot_download_file_from_another_tenant(file_run: FileRun, sqlite_session: Session) -> None:
    file_run.source.tenant_id = OTHER
    sqlite_session.commit()
    with pytest.raises(AgentConfigServiceError) as error:
        _download(file_run.service)
    assert (error.value.code, error.value.status_code) == ("config_file_not_found", 404)
