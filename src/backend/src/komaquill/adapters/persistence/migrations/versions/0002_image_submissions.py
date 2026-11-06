"""Persist image submission idempotency before contacting ComfyUI."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_submissions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("fingerprint", sa.String, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("image_submissions")
