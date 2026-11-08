"""Read preserved conversations without interpreting their old state as current state."""

import json
from uuid import UUID

from sqlalchemy import Select, and_, select

from komaquill.adapters.persistence.connection import Database
from komaquill.adapters.persistence.errors import missing
from komaquill.adapters.persistence.schema import archived_conversations, work_metadata
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


def _visible_archive() -> Select[tuple[str, str, str | None]]:
    return (
        select(archived_conversations.c.id, archived_conversations.c.payload, work_metadata.c.name)
        .select_from(
            archived_conversations.outerjoin(
                work_metadata,
                and_(
                    work_metadata.c.kind == "archived",
                    work_metadata.c.id == archived_conversations.c.id,
                    work_metadata.c.output_index == 0,
                ),
            )
        )
        .where(visible_work("archived", archived_conversations.c.id))
    )


def _named(identity: str, payload: str, name: str | None) -> ArchivedConversation:
    record = decode(identity, payload)
    return record.model_copy(update={"title": name}) if name else record


@stored
def list_conversations(database: Database) -> list[ArchivedConversation]:
    with database.engine.connect() as connection:
        rows = connection.execute(
            _visible_archive()
            .where(archived_conversations.c.reason != "converted")
            .order_by(archived_conversations.c.id)
        ).tuples()
        return [_named(str(identity), str(payload), name) for identity, payload, name in rows]


@stored
def get_conversation(database: Database, identity: UUID) -> ArchivedConversation:
    with database.engine.connect() as connection:
        row = (
            connection.execute(
                _visible_archive().where(
                    archived_conversations.c.id == str(identity),
                    (archived_conversations.c.reason != "converted")
                    | visible_work("conversation", archived_conversations.c.id),
                )
            )
            .tuples()
            .one_or_none()
        )
        if row is None:
            raise missing()
        return _named(str(row[0]), str(row[1]), row[2])
