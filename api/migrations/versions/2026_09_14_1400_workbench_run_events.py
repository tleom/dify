"""Persist ordered workbench progress independently of worker cleanup."""

import sqlalchemy as sa
from alembic import op

from models.types import StringUUID

revision = "wb20260914events"
down_revision = "wb20260914merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workbench_run_events",
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("run_id", StringUUID(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_key", sa.String(128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="wb_event_sequence"),
        sa.UniqueConstraint("run_id", "event_key", name="wb_event_identity"),
    )


def downgrade() -> None:
    # Disable new producers and retain/export event history before schema rollback.
    op.drop_table("workbench_run_events")
