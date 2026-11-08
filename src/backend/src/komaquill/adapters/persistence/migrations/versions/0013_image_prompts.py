"""Retain image prompt preferences at standalone drawing acceptance."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("image_submissions", sa.Column("image_prompts", sa.Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Restore the verified workspace backup to recover the previous schema.")
