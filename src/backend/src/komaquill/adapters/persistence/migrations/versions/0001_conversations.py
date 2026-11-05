"""Create conversation, operation and image metadata tables."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("payload", sa.Text, nullable=False),
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("conversation_id", sa.String, nullable=False),
        sa.Column("request", sa.Text, nullable=False),
        sa.Column("fingerprint", sa.String, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("active_slot", sa.Integer, unique=True),
        sa.Column("owner_pid", sa.Integer, nullable=False),
        sa.Column("owner_started", sa.String, nullable=False),
    )
    op.create_table(
        "images",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("payload", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("images")
    op.drop_table("operations")
    op.drop_table("conversations")
