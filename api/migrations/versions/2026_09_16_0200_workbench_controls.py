"""Persist workbench goal, plan, task list and idempotent slash commands."""

import sqlalchemy as sa
from alembic import op

from models.types import StringUUID

revision = "wb20260916control"
down_revision = "wb20260914events"
branch_labels = None
depends_on = None


def _owner_columns():
    return [
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("tenant_id", StringUUID(), nullable=False),
        sa.Column("account_id", StringUUID(), nullable=False),
        sa.Column("chat_id", StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    ]


def upgrade() -> None:
    op.create_table(
        "workbench_controls",
        *_owner_columns(),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("goal_active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.UniqueConstraint("chat_id", name="wb_control_chat"),
    )
    op.create_index("wb_control_active_goal", "workbench_controls", ["goal_active"])
    op.create_table(
        "workbench_commands",
        *_owner_columns(),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.UniqueConstraint("chat_id", "request_key", name="wb_command_idempotency"),
    )


def downgrade() -> None:
    # Export these user-owned records before an explicitly requested schema rollback.
    op.drop_table("workbench_commands")
    op.drop_index("wb_control_active_goal", table_name="workbench_controls")
    op.drop_table("workbench_controls")
