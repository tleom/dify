"""Persist revocable, optionally expiring short links for owned files."""

import sqlalchemy as sa
from alembic import op

from models.types import StringUUID

revision = "wb20260916share"
down_revision = "wb20260916control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workbench_file_shares",
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("tenant_id", StringUUID(), nullable=False),
        sa.Column("account_id", StringUUID(), nullable=False),
        sa.Column("workspace_id", StringUUID(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("path_hash", sa.String(64), nullable=False),
        sa.Column("token", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.Column("revoked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "account_id", "path_hash", name="wb_share_owner_path"),
        sa.UniqueConstraint("token", name="wb_share_token"),
    )


def downgrade() -> None:
    op.drop_table("workbench_file_shares")
