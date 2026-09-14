import json
from uuid import uuid4

import pytest
from werkzeug.exceptions import Forbidden

from models.workbench import WorkbenchRun
from services.workbench.mentions import load_run_mentions


def test_mentions_are_frozen_and_scoped_to_running_owner(sqlite_session):
    tenant, account = str(uuid4()), str(uuid4())
    run = WorkbenchRun(
        tenant_id=tenant,
        account_id=account,
        chat_id=str(uuid4()),
        revision_id=str(uuid4()),
        request_key="mention-check",
        status="running",
        payload=json.dumps({"resource_mentions": {"skills": ["writing"], "tools": ["tool"]}}),
    )
    sqlite_session.add(run)
    sqlite_session.commit()
    assert load_run_mentions(run.id, tenant, account).skills == ["writing"]
    for wrong_tenant, wrong_account in [(str(uuid4()), account), (tenant, str(uuid4())), (tenant, None)]:
        with pytest.raises(Forbidden):
            load_run_mentions(run.id, wrong_tenant, wrong_account)
    run.status = "succeeded"
    sqlite_session.commit()
    with pytest.raises(Forbidden):
        load_run_mentions(run.id, tenant, account)
