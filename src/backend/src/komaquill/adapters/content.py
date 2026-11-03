"""Keep content labels and operation policy snapshots in the shared database."""

import hashlib
import json
from contextlib import AbstractContextManager
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from komaquill.adapters.storage_errors import stored
from komaquill.domain.content import ContentLabel, ContentRating, ContentUpdate, visible
from komaquill.domain.errors import KomaquillError
from komaquill.library.models import Preferences


class ContentDatabase(Protocol):
    engine: Engine

    def transaction(self) -> AbstractContextManager[Connection]: ...


class ContentPolicy:
    def __init__(self, database: ContentDatabase) -> None:
        self.database = database

    @stored
    def list(self) -> list[ContentLabel]:
        if not self.enabled():
            return []
        tables = {
            "conversation": "conversations",
            "preset": "presets",
            "image": "images",
            "archive": "archived_conversations",
            "job": "creation_jobs",
        }
        values: dict[tuple[str, str], ContentLabel] = {}
        with self.database.engine.connect() as reader:
            for kind, table in tables.items():
                where = (
                    " WHERE deleted=0"
                    if kind == "preset"
                    else (" WHERE reason!='converted'" if kind == "archive" else "")
                )
                for identity, payload in reader.execute(
                    text(f"SELECT id,payload FROM {table}{where}")
                ):
                    data = json.loads(str(payload))
                    values[kind, str(identity)] = ContentLabel.model_validate(
                        {
                            "kind": kind,
                            "id": identity,
                            "rating": self.rating(kind, str(identity), reader),
                            "title": data.get("name", data.get("definition", {}).get("title", "")),
                        }
                    )
            for row in reader.execute(
                text("SELECT kind,id,rating,revision FROM content_labels")
            ).mappings():
                key = row["kind"], row["id"]
                if key in values:
                    values[key] = values[key].model_copy(
                        update={"rating": row["rating"], "revision": row["revision"]}
                    )
        return list(values.values())

    @stored
    def update(self, request: ContentUpdate) -> ContentLabel:
        if not self.enabled():
            raise KomaquillError(code="content_hidden", retryable=False)
        if not any(label.kind == request.kind and label.id == request.id for label in self.list()):
            raise KomaquillError(code="request_not_found", retryable=False)
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        with self.database.transaction() as writer:
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
            revision = row["revision"] if row else 0
            if known is not None:
                if known != fingerprint:
                    raise KomaquillError(code="operation_conflict", retryable=False)
            else:
                if writer.execute(
                    text("SELECT id FROM operations WHERE id=:id"),
                    {"id": str(request.operation_id)},
                ).first():
                    raise KomaquillError(code="operation_conflict", retryable=False)
                if revision != request.expected_revision:
                    raise KomaquillError(code="operation_conflict", retryable=False)
                if row is None:
                    self.set_rating(
                        request.kind,
                        request.id,
                        self.rating(request.kind, request.id, writer),
                        writer,
                    )
                self.set_rating(request.kind, request.id, request.rating, writer)
                writer.execute(
                    text("INSERT INTO command_receipts VALUES(:id,:fingerprint)"),
                    {"id": str(request.operation_id), "fingerprint": fingerprint},
                )
            writer.commit()
        return next(
            (
                label
                for label in self.list()
                if label.id == request.id and label.kind == request.kind
            ),
            ContentLabel(kind=request.kind, id=request.id, rating=request.rating),
        )

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
