"""Record the public resource snapshot separately from a native Binding generation."""
from alembic import op
import sqlalchemy as sa
from models.types import StringUUID

revision = "wb20260910b"
down_revision = "wb20260910"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workbench_revisions", sa.Column("template_snapshot_id", StringUUID(), nullable=True))
    op.execute(sa.text("UPDATE workbench_revisions SET template_snapshot_id = "
        "(SELECT base_snapshot_id FROM workbench_chats WHERE workbench_chats.id = workbench_revisions.chat_id)"))


def downgrade():
    op.drop_column("workbench_revisions", "template_snapshot_id")
