"""Retain accepted execution policy without mutable viewing classifications."""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text

from komaquill.adapters.storage_errors import stored

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine


class ContentPolicy:
    def __init__(self, engine: "Engine") -> None:
        self.engine = engine

    @stored
    def capture(self, identity: UUID, connection: "Connection | None" = None) -> bool:
        if connection is None:
            with self.engine.begin() as writer:
                return self.capture(identity, writer)
        previous = connection.execute(
            text("SELECT nsfw_enabled FROM operation_policies WHERE id=:id"), {"id": str(identity)}
        ).scalar()
        if previous is not None:
            if type(previous) is not int or previous not in {0, 1}:
                raise ValueError("Invalid operation content policy.")
            return bool(previous)
        connection.execute(
            text("INSERT INTO operation_policies(id,nsfw_enabled) VALUES(:id,1)"),
            {"id": str(identity)},
        )
        return True
