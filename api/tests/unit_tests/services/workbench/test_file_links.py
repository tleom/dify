"""Real ownership queries, stable link issuance, and Flask file delivery."""

import base64
from collections.abc import Callable, Iterator
from unittest.mock import Mock
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_restx import Api
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.exceptions import BadRequest, Conflict, Forbidden, NotFound

from controllers.common.schema import query_params_from_model
from controllers.console.workbench import FileLinks, WorkbenchFileLinksQuery
from controllers.files.workbench_files import WorkbenchFileContent
from core.db import session_factory as factory_module
from models.agent import AgentWorkspace, AgentWorkspaceOwnerType
from models.base import TypeBase
from models.workbench import WorkbenchChat, WorkbenchRun, WorkbenchRunEvent
from services.workbench import file_links, files
from services.workbench.preview import AgentFilePreviewPayload, open_preview

type FileSpace = tuple[file_links.AgentFileLinksPayload, str, dict[str, bytes], sessionmaker[Session], FlaskClient]


@pytest.mark.parametrize("query", [{}, {"path": ""}])
def test_single_file_link_route_requires_explicit_nonempty_path(
    query: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = Mock()
    monkeypatch.setattr(file_links, "lookup", lookup)
    assert query_params_from_model(WorkbenchFileLinksQuery)["path"]["required"] is True
    with Flask(__name__).test_request_context("/workbench/files/links", query_string=query):
        with pytest.raises(ValidationError):
            FileLinks().get()
    lookup.assert_not_called()


@pytest.fixture
def file_space(monkeypatch: pytest.MonkeyPatch, config_overrides: Callable[..., None]) -> Iterator[FileSpace]:
    config_overrides(SECRET_KEY="file-link-test-key", FILES_URL="https://files.example.test", WORKBENCH_ENABLED=True)
    engine = create_engine("sqlite://")
    TypeBase.metadata.create_all(
        engine,
        tables=[
            TypeBase.metadata.tables[model.__tablename__]
            for model in (WorkbenchChat, WorkbenchRun, AgentWorkspace, WorkbenchRunEvent)
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(factory_module, "_session_maker", factory)
    tenant, account, app_id, chat_id, run_id, workspace_id = (str(uuid4()) for _ in range(6))
    root = "conversations/" + chat_id
    payload = file_links.AgentFileLinksPayload(
        tenant_id=tenant, account_id=account, app_id=app_id, workbench_run_id=run_id
    )
    with factory.begin() as session:
        session.add(
            WorkbenchChat(
                id=chat_id,
                tenant_id=tenant,
                account_id=account,
                agent_id=str(uuid4()),
                app_id=app_id,
                base_snapshot_id=str(uuid4()),
                title="图表会话",
            )
        )
        session.add(
            WorkbenchRun(
                id=run_id,
                tenant_id=tenant,
                account_id=account,
                chat_id=chat_id,
                revision_id=str(uuid4()),
                request_key="test",
                payload="{}",
                status="running",
            )
        )
        session.add(
            AgentWorkspace(
                id=workspace_id,
                tenant_id=tenant,
                app_id=app_id,
                owner_type=AgentWorkspaceOwnerType.WORKBENCH_USER,
                owner_id=account,
                owner_scope_key="root",
                backend_workspace_ref=workspace_id,
            )
        )
    # Only the sandbox data plane is replaced: authorization and HTTP serialization are real.
    contents = {root + "/图表.png": b"\x89PNG\r\n\x1a\n", root + "/报告.html": b"<script>test()</script>"}

    def manager(workspace: str, action: str, request: dict[str, str]) -> dict[str, object]:
        assert workspace == workspace_id
        assert action == "files"
        if request["operation"] == "mkdir":
            return {}
        if request["operation"] == "list":
            return {
                "path": root,
                "entries": [
                    {
                        "name": path.rsplit("/", 1)[1],
                        "path": path,
                        "kind": "file",
                        "size": len(data),
                        "modified": 1.0,
                        "version": "1",
                        "downloadable": len(data) <= 20 * 1024 * 1024,
                    }
                    for path, data in sorted(contents.items())[:2000]
                ],
            }
        if request["operation"] == "stat":
            path = request["path"]
            if path not in contents:
                return {"path": path, "kind": "missing"}
            return {
                "name": path.rsplit("/", 1)[1],
                "path": path,
                "kind": "file",
                "size": len(contents[path]),
                "modified": 1.0,
                "version": "1",
                "downloadable": len(contents[path]) <= 20 * 1024 * 1024,
            }
        if request["path"] not in contents:
            raise NotFound()
        return {
            "name": request["path"].rsplit("/", 1)[1],
            "kind": "file",
            "version": "1",
            "data": base64.b64encode(contents[request["path"]]).decode(),
        }

    monkeypatch.setattr(files, "ensure_workspace", lambda *_: workspace_id)
    monkeypatch.setattr(files, "manager", manager)
    monkeypatch.setattr(file_links, "manager", manager)
    app = Flask(__name__)
    Api(app).add_resource(WorkbenchFileContent, "/files/workbench/<token>/<filename>")
    yield payload, root, contents, factory, app.test_client()
    engine.dispose()


def test_ui_and_agent_receive_identical_stable_links_and_inline_bytes(file_space: FileSpace) -> None:
    payload, root, contents, _, client = file_space
    ui = files.operate(payload.tenant_id, payload.account_id, "list", root)["entries"]
    agent = file_links.agent_lookup(payload)["entries"]
    assert agent == ui
    assert file_links.agent_lookup(payload)["entries"] == ui
    for item in ui:
        download = client.get(urlsplit(item["download_url"]).path + "?mode=download")
        preview = client.get(urlsplit(item["preview_url"]).path + "?mode=preview")
        assert download.status_code == preview.status_code == 200
        assert download.data == preview.data == contents[item["path"]]
        assert download.headers["Content-Disposition"].startswith("attachment")
        assert preview.headers["Content-Disposition"].startswith("inline")
        assert preview.headers["Content-Security-Policy"] == "sandbox allow-scripts allow-downloads"
        assert preview.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get(urlsplit(ui[0]["preview_url"]).path + "?mode=preview").mimetype == "image/png"
    specific = file_links.agent_lookup(payload.model_copy(update={"path": "/workspace/" + ui[0]["path"]}))
    assert specific["entries"] == ui[:1]


def test_preview_records_one_owned_event_and_retries_do_not_reopen(
    file_space: FileSpace, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, root, _, factory, _ = file_space
    notify = Mock()
    monkeypatch.setattr("services.workbench.preview.notify", notify)
    with factory.begin() as session:
        run = session.get(WorkbenchRun, payload.workbench_run_id)
        assert run is not None
        run.backend_run_id = "native-preview"
    request = AgentFilePreviewPayload(
        **payload.model_dump(exclude={"path"}), path="图表.png", backend_run_id="native-preview", request_key="one"
    )
    first = open_preview(request)
    assert first == open_preview(request)
    assert first["accepted"] is True
    assert first["file"]["path"] == root + "/图表.png"
    assert "download_url" not in first["file"]
    with factory() as session:
        events = session.scalars(select(WorkbenchRunEvent)).all()
        assert len(events) == 1
        assert events[0].sequence == 1
    with pytest.raises(Conflict):
        open_preview(request.model_copy(update={"path": "报告.html"}))
    with pytest.raises(Forbidden):
        open_preview(request.model_copy(update={"backend_run_id": "old-execution"}))
    with pytest.raises(Forbidden):
        open_preview(request.model_copy(update={"account_id": "another-account"}))
    with pytest.raises(BadRequest):
        open_preview(request.model_copy(update={"path": "."}))
    with factory.begin() as session:
        run = session.get(WorkbenchRun, payload.workbench_run_id)
        assert run is not None
        run.status = "completed"
    with pytest.raises(Forbidden):
        open_preview(request)


def test_signature_tamper_owner_mismatch_missing_file_and_deleted_chat_revoke(file_space: FileSpace) -> None:
    payload, root, contents, factory, client = file_space
    entry = file_links.agent_lookup(payload)["entries"][0]
    url = urlsplit(entry["download_url"]).path
    token = url.split("/")[3]
    forged = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert client.get(url.replace(token, forged)).status_code == 404
    with pytest.raises(Forbidden):
        file_links.agent_lookup(payload.model_copy(update={"account_id": str(uuid4())}))
    with pytest.raises(NotFound):
        file_links.agent_lookup(payload.model_copy(update={"path": "missing.png"}))
    with pytest.raises(NotFound, match="不属于当前用户"):
        file_links.agent_lookup(payload.model_copy(update={"path": "conversations/another/file.png"}))
    removed = contents.pop(entry["path"])
    assert client.get(url).status_code == 404
    contents[entry["path"]] = removed
    with factory.begin() as session:
        chat = session.get(WorkbenchChat, root.split("/")[1])
        assert chat is not None
        chat.deleted = 1
    assert client.get(url).status_code == 404


def test_specific_path_lookup_reaches_files_beyond_directory_limit(file_space: FileSpace) -> None:
    payload, root, contents, _, client = file_space
    for index in range(2000):
        contents[f"{root}/a-{index:04}.txt"] = b"entry"
    path = root + "/z-last.png"
    contents[path] = b"\x89PNG\r\n\x1a\n"
    listing = files.operate(payload.tenant_id, payload.account_id, "list", root)
    assert len(listing["entries"]) == 2000
    assert all(entry["path"] != path for entry in listing["entries"])
    assert file_links.agent_lookup(payload)["complete"] is False
    specific = file_links.agent_lookup(payload.model_copy(update={"path": "z-last.png"}))
    assert specific["complete"] is True
    assert len(specific["entries"]) == 1
    entry = specific["entries"][0]
    assert entry["path"] == path
    assert client.get(urlsplit(entry["download_url"]).path).data == contents[path]


def test_configured_public_origin_is_used_without_changing_signed_identity(
    file_space: FileSpace, config_overrides: Callable[..., None]
) -> None:
    payload, _, _, _, _ = file_space
    original = file_links.agent_lookup(payload)["entries"][0]
    config_overrides(FILES_URL="https://agent.xcmggx.com")
    public = file_links.agent_lookup(payload)["entries"][0]
    for mode in ("preview_url", "download_url"):
        assert public[mode] == original[mode].replace("https://files.example.test", "https://agent.xcmggx.com")


def test_oversized_files_remain_visible_without_unusable_links(file_space: FileSpace) -> None:
    payload, root, contents, _, _ = file_space
    path = root + "/large.bin"
    contents[path] = b"x" * (20 * 1024 * 1024 + 1)
    ui = files.operate(payload.tenant_id, payload.account_id, "list", root)["entries"]
    agent = file_links.agent_lookup(payload)["entries"]
    specific = file_links.agent_lookup(payload.model_copy(update={"path": "large.bin"}))["entries"]
    for entries in (ui, agent, specific):
        entry = next(item for item in entries if item["path"] == path)
        assert entry["downloadable"] is False
        assert "download_url" not in entry
        assert "preview_url" not in entry
    with pytest.raises(BadRequest, match="超出下载范围"):
        file_links.lookup(payload.tenant_id, payload.account_id, path)
