"""Retain original payloads and mark ambiguous visual definitions for explicit review."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "command_receipts",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("fingerprint", sa.String, nullable=False),
    )
    op.create_table(
        "state_migration_originals",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("kind", sa.String, primary_key=True),
        sa.Column("payload", sa.Text, nullable=False),
    )
    connection = op.get_bind()
    for table in ("conversations", "presets"):
        for identity, payload in connection.execute(
            sa.text(f"SELECT id,payload FROM {table}")
        ).all():
            value = json.loads(str(payload))
            connection.execute(
                sa.text(
                    "INSERT INTO state_migration_originals(id,kind,payload) "
                    "VALUES(:id,:kind,:payload)"
                ),
                {"id": identity, "kind": table, "payload": payload},
            )
            if table == "conversations":
                for key in ("user", "character"):
                    person = value["definition"][key]
                    person["visual_review_required"] = bool(person.get("appearance"))
                value["state"]["character_place"] = value["state"].get("place", "")
            elif "appearance" in value:
                value["visual_review_required"] = bool(value["appearance"])
            connection.execute(
                sa.text(f"UPDATE {table} SET payload=:payload WHERE id=:id"),
                {"id": identity, "payload": json.dumps(value, ensure_ascii=False)},
            )


def downgrade() -> None:
    raise RuntimeError("Restore the recorded database backup instead of downgrading.")
