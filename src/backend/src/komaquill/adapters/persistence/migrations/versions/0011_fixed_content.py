"""Remove mutable content classification while retaining original settings."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for identity, payload in connection.execute(sa.text("SELECT id,payload FROM preferences")):
        value = json.loads(str(payload))
        if "nsfw_enabled" not in value:
            continue
        connection.execute(
            sa.text(
                "INSERT INTO execution_migration_originals(kind,id,payload) "
                "VALUES('preferences',:id,:payload)"
            ),
            {"id": str(identity), "payload": payload},
        )
        del value["nsfw_enabled"]
        connection.execute(
            sa.text("UPDATE preferences SET payload=:payload WHERE id=:id"),
            {"id": identity, "payload": json.dumps(value, ensure_ascii=False)},
        )
    op.drop_table("content_labels")
