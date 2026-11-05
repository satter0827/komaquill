"""Retain validated replies and the revision used by each conversation operation."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operations", sa.Column("base_revision", sa.Integer))
    op.add_column("operations", sa.Column("reply", sa.Text))


def downgrade() -> None:
    with op.batch_alter_table("operations") as batch:
        batch.drop_column("reply")
        batch.drop_column("base_revision")
