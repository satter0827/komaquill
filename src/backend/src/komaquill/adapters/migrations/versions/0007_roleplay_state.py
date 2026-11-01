"""Preserve original conversations while upgrading explicit roleplay contracts."""

import json
from typing import Any

import sqlalchemy as sa
from alembic import op
from komaquill.conversations.models import Conversation
from komaquill.domain.roleplay import ConversationState

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def state(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value[key]
        for key in ("place", "time", "goal", "relationship", "player", "character")
        if key in value
    }
    for key, item in value.items():
        if key in result:
            continue
        person, _, field = key.partition("_")
        if person in {"player", "character"} and field in {
            "clothing",
            "pose",
            "expression",
            "appearance",
        }:
            result.setdefault(person, {})[field] = item
        elif key == "character_location" and (not result.get("place") or result["place"] == item):
            result["place"] = item
        elif key == "sample" and item == "greeted":
            continue
        else:
            raise ValueError("An old state field has no unambiguous mapping.")
    return ConversationState.model_validate(result).model_dump(mode="json")


def converted(payload: str) -> str:
    value = json.loads(payload)
    if "created_at" not in value or any(
        "created_at" not in turn for turn in value.get("turns", [])
    ):
        raise ValueError("Missing historical timestamp.")
    definition = value["definition"]
    for key in ("scenario", "character", "user"):
        raw = definition.get(key, "")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            definition[key] = parsed if isinstance(parsed, dict) else {"description": raw}
            definition[key].pop("character_id", None)
    definition.pop("images", None)
    definition["initial_state"] = state(definition.get("initial_state", {}))
    value["state"] = state(value.get("state", {}))
    for turn in value.get("turns", []):
        turn.pop("scene", None)
        turn["state"] = state(turn.get("state", {}))
    return Conversation.model_validate(value).model_dump_json()


def upgrade() -> None:
    op.create_table(
        "preset_saves",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_table(
        "archived_conversations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
    )
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT id,payload FROM conversations")).all():
        identity, payload = str(row[0]), str(row[1])
        try:
            Conversation.model_validate_json(payload)
        except ValueError:
            try:
                updated = converted(payload)
                reason = "converted"
            except (ValueError, KeyError, TypeError, AttributeError):
                updated = None
                reason = "legacy_state"
            connection.execute(
                sa.text(
                    "INSERT INTO archived_conversations(id,payload,reason) "
                    "VALUES(:id,:payload,:reason)"
                ),
                {"id": identity, "payload": payload, "reason": reason},
            )
            if updated is None:
                connection.execute(
                    sa.text("DELETE FROM conversations WHERE id=:id"), {"id": identity}
                )
            else:
                connection.execute(
                    sa.text("UPDATE conversations SET payload=:payload WHERE id=:id"),
                    {"id": identity, "payload": updated},
                )


def downgrade() -> None:
    raise RuntimeError("Restore the recorded database backup instead of downgrading.")
