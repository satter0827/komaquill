"""Add work names, trash and detached display references."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_metadata",
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("output_index", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("trashed", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "image_detachments",
        sa.Column("owner_kind", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.String(), primary_key=True),
        sa.Column("slot", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("output_index", sa.Integer(), primary_key=True),
    )


def downgrade() -> None:
    raise RuntimeError("Restore the verified workspace backup to recover the previous schema.")
