"""Add reusable settings and durable creation operations without rewriting conversations."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "presets",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("deleted", sa.Boolean, nullable=False, server_default="0"),
    )
    op.create_table(
        "preferences",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("payload", sa.Text, nullable=False),
    )
    op.create_table(
        "creation_jobs",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("owner_pid", sa.Integer, nullable=False),
        sa.Column("owner_started", sa.String, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("creation_jobs")
    op.drop_table("preferences")
    op.drop_table("presets")
