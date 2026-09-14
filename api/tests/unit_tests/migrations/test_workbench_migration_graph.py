from pathlib import Path

from alembic.script import ScriptDirectory


def test_default_upgrade_head_includes_workbench_and_upstream() -> None:
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[3] / "migrations"))

    # The deployment entrypoint upgrades to head, which must resolve without choosing a branch.
    assert scripts.get_current_head() is not None
    ancestors = {revision.revision for revision in scripts.walk_revisions(base="base", head="head")}
    assert {"wb20260911u171", "d8e4a6b1c902"} <= ancestors
