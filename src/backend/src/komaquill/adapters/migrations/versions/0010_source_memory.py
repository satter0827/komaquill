"""Preserve former summaries before adopting source-backed memory."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for identity, payload in connection.execute(
        sa.text("SELECT id,payload FROM conversations")
    ).all():
        value = json.loads(str(payload))
        if not isinstance(value, dict):
            raise ValueError("The conversation header must be an object.")
        if value.get("memory") is None:
            continue
        connection.execute(
            sa.text(
                "INSERT INTO execution_migration_originals(kind,id,payload) "
                "VALUES('conversation_memory',:id,:payload)"
            ),
            {"id": identity, "payload": payload},
        )
        value["memory"] = None
        connection.execute(
            sa.text("UPDATE conversations SET payload=:payload WHERE id=:id"),
            {"id": identity, "payload": json.dumps(value, ensure_ascii=False)},
        )
