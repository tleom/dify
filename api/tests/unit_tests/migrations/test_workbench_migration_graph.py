from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import mysql, postgresql

from models.workbench import WorkbenchRunEvent


def test_default_upgrade_head_includes_workbench_and_upstream() -> None:
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[3] / "migrations"))

    # The deployment entrypoint upgrades to head, which must resolve without choosing a branch.
    assert scripts.get_current_head() is not None
    ancestors = {revision.revision for revision in scripts.walk_revisions(base="base", head="head")}
    assert {"wb20260911u171", "d8e4a6b1c902", "wb20260914events"} <= ancestors


@pytest.mark.parametrize(("dialect_name", "payload_type"), [("mysql", "LONGTEXT"), ("postgresql", "TEXT")])
def test_journal_payload_supports_large_tool_output_on_each_database(dialect_name, payload_type):
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[3] / "migrations"))
    migration = scripts.get_revision("wb20260914events")
    output = StringIO()
    context = MigrationContext.configure(dialect_name=dialect_name, opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration.module.upgrade()
    assert f"payload {payload_type} NOT NULL" in output.getvalue()
    dialect = mysql.dialect() if dialect_name == "mysql" else postgresql.dialect()
    assert WorkbenchRunEvent.__table__.c.payload.type.compile(dialect=dialect) == payload_type
