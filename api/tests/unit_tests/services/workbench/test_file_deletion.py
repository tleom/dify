from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import Table, create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.exceptions import Conflict

from models.workbench import WorkbenchRun
from services.workbench import directories, files


@pytest.mark.parametrize("status", ["waiting_input", "environment_update", "completed"])
@pytest.mark.parametrize("suffix", ["", "/source.txt"])
def test_resumable_runs_protect_conversation_files(status: str, suffix: str, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite://")
    table = WorkbenchRun.__table__
    assert isinstance(table, Table)
    table.create(engine)
    factory = sessionmaker(bind=engine)
    chat_id, tenant_id, account_id = (str(uuid4()) for _ in range(3))
    root = f"conversations/{chat_id}"
    with factory.begin() as session:
        session.add(
            WorkbenchRun(
                id=str(uuid4()),
                tenant_id=tenant_id,
                account_id=account_id,
                chat_id=chat_id,
                revision_id=str(uuid4()),
                request_key="request",
                payload="{}",
                status=status,
            )
        )
    monkeypatch.setattr(files.session_factory, "create_session", factory)
    monkeypatch.setattr(files, "ensure_workspace", lambda *_args: "workspace")
    monkeypatch.setattr(
        directories,
        "resolve_path",
        lambda *_args, **_kwargs: (
            root,
            SimpleNamespace(id=chat_id, title="conversation"),
        ),
    )
    manager = Mock(return_value={})
    monkeypatch.setattr(files, "manager", manager)
    try:
        if status == "completed":
            files.operate(tenant_id, account_id, "delete", root + suffix)
            assert manager.call_args.args[2]["operation"] == "delete"
        else:
            with pytest.raises(Conflict):
                files.operate(tenant_id, account_id, "delete", root + suffix)
            manager.assert_not_called()
    finally:
        engine.dispose()
