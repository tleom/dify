"""Persist personal workbench conversation pins."""
from alembic import op
import sqlalchemy as sa

revision = "wb20260911"
down_revision = "wb20260910b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workbench_chats", sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("workbench_chats", "pinned")
