"""Add account-owned Agent workbench records."""
from alembic import op
import sqlalchemy as sa
from models.types import StringUUID

revision = "wb20260910"
down_revision = "a4f8d2c9e1b0"
branch_labels = None
depends_on = None

def upgrade():
    def identity():
        return [sa.Column("id", StringUUID(), primary_key=True),
            sa.Column("tenant_id", StringUUID(), nullable=False), sa.Column("account_id", StringUUID(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp())]
    op.create_table("workbench_chats", *identity(),
        sa.Column("agent_id", StringUUID(), nullable=False), sa.Column("app_id", StringUUID(), nullable=False),
        sa.Column("base_snapshot_id", StringUUID(), nullable=False), sa.Column("conversation_id", StringUUID()),
        sa.Column("title", sa.String(255), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False))
    op.create_index("wb_chat_owner", "workbench_chats", ["tenant_id", "account_id"])
    op.create_table("workbench_revisions", *identity(),
        sa.Column("chat_id", StringUUID(), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("selection", sa.Text(), nullable=False), sa.Column("effective_soul", sa.Text(), nullable=False),
        sa.UniqueConstraint("chat_id", "version", name="wb_revision_version"))
    op.create_table("workbench_runs", *identity(),
        sa.Column("chat_id", StringUUID(), nullable=False), sa.Column("revision_id", StringUUID(), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False), sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("task_id", StringUUID()),
        sa.Column("backend_run_id", StringUUID()), sa.Column("error", sa.Text()),
        sa.Column("event_log", sa.Text(), nullable=False),
        sa.UniqueConstraint("chat_id", "request_key", name="wb_run_idempotency"))
    op.create_index("wb_run_state", "workbench_runs", ["status"])

def downgrade():
    for name in ("workbench_runs", "workbench_revisions", "workbench_chats"):
        op.drop_table(name)
