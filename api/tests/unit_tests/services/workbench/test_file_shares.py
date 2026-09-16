"""Public links preserve owner isolation, live file bytes, and revocation."""

from contextlib import nullcontext
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
from flask import Flask
from flask_restx import Api
from werkzeug.exceptions import NotFound

from controllers.files.workbench_files import WorkbenchSharedFile
from models.workbench import WorkbenchChat, WorkbenchFileShare
from services.workbench import file_shares, files
from tests.unit_tests.services.workbench.test_file_links import FileSpace
from tests.unit_tests.services.workbench.test_file_links import file_space as file_space  # noqa: PLC0414 - pytest fixture registration


@pytest.fixture
def shares(file_space: FileSpace, monkeypatch: pytest.MonkeyPatch):
    payload, root, contents, factory, _ = file_space
    WorkbenchFileShare.__table__.create(factory.kw["bind"])
    monkeypatch.setattr(file_shares, "ensure_workspace", files.ensure_workspace)
    monkeypatch.setattr(file_shares, "manager", files.manager)
    monkeypatch.setattr(file_shares, "redis_client", Mock(lock=lambda *_args, **_kwargs: nullcontext()))
    app = Flask(__name__)
    Api(app).add_resource(WorkbenchSharedFile, "/files/s/<token>")
    return payload, root, contents, factory, app.test_client()


def test_permanent_html_link_reads_latest_bytes_and_revocation(shares):
    owner, root, contents, _, client = shares
    path = root + "/报告.html"
    share = file_shares.create_share(owner.tenant_id, owner.account_id, path)
    assert share["expires_at"] is None
    assert len(share["url"].rsplit("/", 1)[1]) == 24
    assert file_shares.get_share(owner.tenant_id, owner.account_id, path) == share
    route = urlsplit(share["url"]).path
    response = client.get(route)
    assert response.status_code == 200
    assert response.data == contents[path]
    assert response.mimetype == "text/html"
    assert response.headers["Content-Disposition"].startswith("inline")
    assert response.headers["Content-Security-Policy"] == "sandbox allow-scripts allow-downloads"
    assert response.headers["Cache-Control"] == "private, no-store"
    contents[path] = b"<h1>Updated</h1>"
    assert client.get(route).data == contents[path]
    file_shares.revoke_share(owner.tenant_id, "other-account", path)
    assert client.get(route).status_code == 200
    file_shares.revoke_share(owner.tenant_id, owner.account_id, path)
    assert client.get(route).status_code == 404
    assert file_shares.get_share(owner.tenant_id, owner.account_id, path) is None


def test_foreign_owner_expiry_rotation_and_deleted_chat(shares, monkeypatch: pytest.MonkeyPatch):
    owner, root, _, factory, client = shares
    path = root + "/报告.html"
    with pytest.raises(NotFound):
        file_shares.create_share(owner.tenant_id, "other-account", path)
    with pytest.raises(NotFound):
        file_shares.get_share(owner.tenant_id, "other-account", path)
    first = file_shares.create_share(owner.tenant_id, owner.account_id, path, 1)
    second = file_shares.create_share(owner.tenant_id, owner.account_id, path, 1)
    assert first["url"] != second["url"]
    assert client.get(urlsplit(first["url"]).path).status_code == 404
    route = urlsplit(second["url"]).path
    with monkeypatch.context() as expired:
        expired.setattr(file_shares.time, "time", lambda: second["expires_at"])
        assert client.get(route).status_code == 404
    assert client.get(route).status_code == 200
    with factory.begin() as session:
        chat = session.get(WorkbenchChat, root.split("/")[1])
        assert chat is not None
        chat.deleted = 1
    assert client.get(route).status_code == 404
