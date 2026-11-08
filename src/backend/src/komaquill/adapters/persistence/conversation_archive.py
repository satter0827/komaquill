"""Read preserved conversations without interpreting their old state as current state."""

import json
from uuid import UUID

from sqlalchemy import select, text

from komaquill.adapters.persistence.connection import Database
from komaquill.adapters.persistence.errors import missing
from komaquill.adapters.persistence.schema import archived_conversations
from komaquill.adapters.persistence.works import visible_work
from komaquill.adapters.storage_errors import stored
from komaquill.domain.roleplay import Record


class ArchivedTurn(Record):
    user: str
    assistant: str
    narration: str = ""


class ArchivedConversation(Record):
    id: UUID
    title: str
    turns: list[ArchivedTurn]
    readable: bool = True


def decode(identity: str, payload: str) -> ArchivedConversation:
    try:
        value = json.loads(payload)
        return ArchivedConversation(
            id=UUID(identity),
            title=value["definition"]["title"],
            turns=[
                ArchivedTurn.model_validate(
                    {key: turn.get(key, "") for key in ("user", "assistant", "narration")}
                )
                for turn in value.get("turns", [])
            ],
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return ArchivedConversation(id=UUID(identity), title=identity, turns=[], readable=False)


@stored
def list_conversations(database: Database) -> list[ArchivedConversation]:
    with database.engine.connect() as connection:
        return [
            decode(str(row[0]), str(row[1]))
            for row in connection.execute(
                select(archived_conversations.c.id, archived_conversations.c.payload)
                .where(
                    archived_conversations.c.reason != "converted",
                    visible_work("archived", archived_conversations.c.id),
                )
                .order_by(archived_conversations.c.id)
            )
        ]


@stored
def get_conversation(database: Database, identity: UUID) -> ArchivedConversation:
    with database.engine.connect() as connection:
        payload = connection.execute(
            text("SELECT payload FROM archived_conversations WHERE id=:id"), {"id": str(identity)}
        ).scalar_one_or_none()
        if payload is None:
            raise missing()
        return decode(str(identity), payload)
