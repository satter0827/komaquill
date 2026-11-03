"""Keep content labels and operation policy snapshots in the shared database."""

import hashlib
import json
from contextlib import AbstractContextManager
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from komaquill.adapters.storage_errors import stored
from komaquill.domain.content import (
    ContentKind,
    ContentLabel,
    ContentRating,
    ContentUpdate,
    visible,
)
from komaquill.domain.errors import KomaquillError
from komaquill.library.models import Preferences

CONTENT_TABLES: dict[ContentKind, str] = {
    "conversation": "conversations",
    "preset": "presets",
    "image": "images",
    "archive": "archived_conversations",
    "job": "creation_jobs",
}


def target_filter(kind: ContentKind) -> str:
    if kind == "preset":
        return " AND t.deleted=0"
    return " AND t.reason!='converted'" if kind == "archive" else ""


def describe(
    kind: ContentKind, identity: str, payload: str, rating: object, revision: object
) -> ContentLabel:
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("Invalid display metadata.")
        title = data.get("name")
        if title is None:
            definition = data.get("definition", {})
            if not isinstance(definition, dict):
                raise ValueError("Invalid display metadata.")
            title = definition.get("title", "")
        if not isinstance(title, str):
            raise ValueError("Invalid display metadata.")
        return ContentLabel.model_validate(
            {
                "kind": kind,
                "id": identity,
                "rating": rating
                if rating is not None
                else ("unclassified" if kind == "archive" else "general"),
                "revision": revision if revision is not None else 0,
                "title": title,
            }
        )
    except (ValueError, TypeError):
        return ContentLabel.model_validate(
            {"kind": kind, "id": identity, "rating": "unclassified", "readable": False}
        )


class ContentDatabase(Protocol):
    engine: Engine

    def transaction(self) -> AbstractContextManager[Connection]: ...


class ContentPolicy:
    def __init__(self, database: ContentDatabase) -> None:
        self.database = database

    @stored
    def list(self) -> list[ContentLabel]:
        with self.database.engine.connect() as reader:
            if not self.enabled(reader):
                return []
            values: list[ContentLabel] = []
            for kind, table in CONTENT_TABLES.items():
                rows = reader.execute(
                    text(
                        f"SELECT t.id,t.payload,c.rating,c.revision FROM {table} AS t "
                        "LEFT JOIN content_labels AS c ON c.id=t.id AND c.kind=:kind "
                        "WHERE 1=1" + target_filter(kind)
                    ),
                    {"kind": kind},
                )
                values.extend(
                    describe(kind, str(row.id), str(row.payload), row.rating, row.revision)
                    for row in rows
                )
            return values

    @stored
    def update(self, request: ContentUpdate) -> ContentLabel:
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        with self.database.transaction() as writer:
            if not self.enabled(writer):
                raise KomaquillError(code="content_hidden", retryable=False)
            target = writer.execute(
                text(
                    f"SELECT t.payload FROM {CONTENT_TABLES[request.kind]} AS t WHERE t.id=:id"
                    + target_filter(request.kind)
                ),
                {"id": str(request.id)},
            ).scalar_one_or_none()
            if target is None:
                raise KomaquillError(code="request_not_found", retryable=False)
            known = writer.execute(
                text("SELECT fingerprint FROM command_receipts WHERE id=:id"),
                {"id": str(request.operation_id)},
            ).scalar()
            row = (
                writer.execute(
                    text("SELECT rating,revision FROM content_labels WHERE kind=:kind AND id=:id"),
                    {"kind": request.kind, "id": str(request.id)},
                )
                .mappings()
                .one_or_none()
            )
            label = describe(
                request.kind,
                str(request.id),
                str(target),
                row["rating"] if row else None,
                row["revision"] if row else None,
            )
            if not label.readable:
                raise KomaquillError(code="storage_failed", stage="storage", retryable=False)
            if known is not None:
                if known != fingerprint:
                    raise KomaquillError(code="operation_conflict", retryable=False)
            else:
                if writer.execute(
                    text("SELECT id FROM operations WHERE id=:id"),
                    {"id": str(request.operation_id)},
                ).first():
                    raise KomaquillError(code="operation_conflict", retryable=False)
                if label.revision != request.expected_revision:
                    raise KomaquillError(code="operation_conflict", retryable=False)
                if row is None:
                    self.set_rating(request.kind, request.id, label.rating, writer)
                self.set_rating(request.kind, request.id, request.rating, writer)
                writer.execute(
                    text("INSERT INTO command_receipts VALUES(:id,:fingerprint)"),
                    {"id": str(request.operation_id), "fingerprint": fingerprint},
                )
            current = (
                writer.execute(
                    text("SELECT rating,revision FROM content_labels WHERE kind=:kind AND id=:id"),
                    {"kind": request.kind, "id": str(request.id)},
                )
                .mappings()
                .one()
            )
            result = describe(
                request.kind, str(request.id), str(target), current["rating"], current["revision"]
            )
            writer.commit()
            return result

    @stored
    def enabled(self, connection: "Connection | None" = None) -> bool:
        if connection is None:
            with self.database.engine.connect() as reader:
                return self.enabled(reader)
        payload = connection.execute(text("SELECT payload FROM preferences WHERE id=1")).scalar()
        return (
            Preferences.model_validate_json(payload, strict=True).nsfw_enabled if payload else False
        )

    @stored
    def rating(
        self, kind: str, identity: UUID | str, connection: "Connection | None" = None
    ) -> ContentRating:
        if connection is None:
            with self.database.engine.connect() as reader:
                return self.rating(kind, identity, reader)
        value = connection.execute(
            text("SELECT rating FROM content_labels WHERE kind=:kind AND id=:id"),
            {"kind": kind, "id": str(identity)},
        ).scalar()
        if value is None:
            return "unclassified" if kind == "archive" else "general"
        if value not in {"general", "nsfw", "unclassified"}:
            raise ValueError("Invalid content classification.")
        return cast(ContentRating, value)

    @stored
    def require(
        self, kind: str, identity: UUID | str, connection: "Connection | None" = None
    ) -> None:
        if not visible(self.rating(kind, identity, connection), self.enabled(connection)):
            raise KomaquillError(code="content_hidden", retryable=False)

    @stored
    def set_rating(
        self,
        kind: str,
        identity: UUID | str,
        rating: ContentRating,
        connection: "Connection | None" = None,
    ) -> None:
        if connection is None:
            with self.database.engine.begin() as writer:
                self.set_rating(kind, identity, rating, writer)
            return
        connection.execute(
            text(
                "INSERT INTO content_labels(kind,id,rating) VALUES(:kind,:id,:rating) "
                "ON CONFLICT(kind,id) DO UPDATE SET rating=excluded.rating, "
                "revision=content_labels.revision+1 "
                "WHERE content_labels.rating<>excluded.rating"
            ),
            {"kind": kind, "id": str(identity), "rating": rating},
        )

    @stored
    def capture(self, identity: UUID, connection: "Connection | None" = None) -> bool:
        if connection is None:
            with self.database.engine.begin() as writer:
                return self.capture(identity, writer)
        previous = connection.execute(
            text("SELECT nsfw_enabled FROM operation_policies WHERE id=:id"), {"id": str(identity)}
        ).scalar()
        if previous is not None:
            if type(previous) is not int or previous not in {0, 1}:
                raise ValueError("Invalid operation content policy.")
            return bool(previous)
        enabled = self.enabled(connection)
        connection.execute(
            text("INSERT INTO operation_policies(id,nsfw_enabled) VALUES(:id,:enabled)"),
            {"id": str(identity), "enabled": int(enabled)},
        )
        return enabled
