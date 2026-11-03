"""Separate immutable turns, execution policy and content classifications."""

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op
from komaquill.conversations.history import advance_history

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def physical_objects(value: Any, identity: str, role: str = "person") -> Any:
    if isinstance(value, list):
        return [physical_objects(item, identity, role) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"held_objects", "initial_held_objects"} and isinstance(item, list):
            result[key] = [
                {"id": uuid5(NAMESPACE_URL, f"{identity}:{role}:{index}:{name}").hex, "name": name}
                if isinstance(name, str)
                else name
                for index, name in enumerate(item)
            ]
        else:
            person = (
                "player"
                if key in {"user", "player"} or value.get("kind") == "prepare"
                else ("character" if key == "character" else role)
            )
            result[key] = physical_objects(item, identity, person)
    return result


def upgrade() -> None:
    op.create_table(
        "content_labels",
        sa.Column("kind", sa.String, primary_key=True),
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("rating", sa.String, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_table(
        "operation_policies",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("nsfw_enabled", sa.Integer, nullable=False),
    )
    op.create_table(
        "conversation_turns",
        sa.Column("conversation_id", sa.String, primary_key=True),
        sa.Column("position", sa.Integer, primary_key=True),
        sa.Column("turn_id", sa.String, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("prefix_sha256", sa.String, nullable=False, server_default=""),
    )
    op.create_table(
        "conversation_events",
        sa.Column("conversation_id", sa.String, primary_key=True),
        sa.Column("kind", sa.String, primary_key=True),
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("pending", sa.Integer, nullable=False),
    )
    op.create_index(
        "ix_conversation_events_current", "conversation_events", ["conversation_id", "pending"]
    )
    op.create_index(
        "ix_conversation_turn_identity",
        "conversation_turns",
        ["conversation_id", "turn_id"],
        unique=True,
    )
    op.create_table(
        "conversation_bookmarks",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("conversation_id", sa.String, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
    )
    op.create_table(
        "execution_migration_originals",
        sa.Column("kind", sa.String, primary_key=True),
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("payload", sa.Text, nullable=False),
    )
    connection = op.get_bind()
    for table, kind in (
        ("conversations", "conversation"),
        ("presets", "preset"),
        ("images", "image"),
        ("archived_conversations", "archive"),
        ("creation_jobs", "job"),
    ):
        for identity, payload in connection.execute(
            sa.text(f"SELECT id,payload FROM {table}")
        ).all():
            connection.execute(
                sa.text("INSERT INTO execution_migration_originals VALUES(:kind,:id,:payload)"),
                {"kind": kind, "id": identity, "payload": payload},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO content_labels(kind,id,rating) VALUES(:kind,:id,'unclassified')"
                ),
                {"kind": kind, "id": identity},
            )
            if table in {"conversations", "presets"}:
                original = json.loads(str(payload))
                value = physical_objects(original, str(identity))
                if table == "conversations" and value != original:
                    value["memory"] = None
                if table == "presets":
                    connection.execute(
                        sa.text("UPDATE presets SET payload=:payload WHERE id=:id"),
                        {"id": identity, "payload": json.dumps(value, ensure_ascii=False)},
                    )
                    continue
                actions = {a["request"]["operation_id"]: a for a in value.get("actions", [])}
                for event_kind in ("actions", "proposals", "visual_reviews"):
                    for event in value.pop(event_kind, []):
                        event_id = (
                            event["id"]
                            if event_kind == "proposals"
                            else event["request"]["operation_id"]
                        )
                        connection.execute(
                            sa.text(
                                "INSERT INTO conversation_events "
                                "VALUES(:conversation,:kind,:id,:payload,:pending)"
                            ),
                            {
                                "conversation": identity,
                                "kind": event_kind,
                                "id": event_id,
                                "payload": json.dumps(event, ensure_ascii=False),
                                "pending": int(
                                    event_kind == "proposals" and event.get("state") == "pending"
                                ),
                            },
                        )
                turns = value.pop("turns", [])
                value["turn_count"] = len(turns)
                prefix = ""
                for position, turn in enumerate(turns):
                    if turn.get("action_id") in actions:
                        turn["action"] = actions[turn["action_id"]]["request"]["action"]
                    prefix = advance_history(prefix, turn)
                    connection.execute(
                        sa.text(
                            "INSERT INTO conversation_turns "
                            "VALUES(:id,:position,:turn,:payload,:prefix)"
                        ),
                        {
                            "id": identity,
                            "position": position,
                            "turn": turn["operation_id"],
                            "payload": json.dumps(turn, ensure_ascii=False),
                            "prefix": prefix,
                        },
                    )
                connection.execute(
                    sa.text("UPDATE conversations SET payload=:payload WHERE id=:id"),
                    {"id": identity, "payload": json.dumps(value, ensure_ascii=False)},
                )

    for row in connection.execute(
        sa.text("SELECT id,reply FROM operations WHERE reply IS NOT NULL")
    ).all():
        connection.execute(
            sa.text(
                "INSERT INTO execution_migration_originals VALUES('operation_reply',:id,:payload)"
            ),
            {"id": row[0], "payload": row[1]},
        )
    op.drop_column("operations", "reply")


def downgrade() -> None:
    raise RuntimeError("Restore the recorded database backup instead of downgrading.")
