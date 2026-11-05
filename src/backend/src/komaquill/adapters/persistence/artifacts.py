"""Bind portable keys and append-only call revisions to immutable local objects."""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert

from komaquill.adapters.object_store import ObjectStore
from komaquill.adapters.paths import artifact_path
from komaquill.adapters.persistence.connection import Database
from komaquill.adapters.persistence.schema import artifact_bindings, llm_records, objects
from komaquill.adapters.storage_errors import stored
from komaquill.domain.artifacts import ObjectRef
from komaquill.domain.errors import KomaquillError


class ArtifactRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.objects = ObjectStore(database.path.parent)

    @stored
    def keys(self, prefix: str) -> list[str]:
        with self.database.engine.connect() as connection:
            return list(
                connection.execute(
                    select(artifact_bindings.c.key).where(
                        artifact_bindings.c.key.startswith(prefix, autoescape=True)
                    )
                ).scalars()
            )

    @stored
    def reference(self, key: str) -> ObjectRef | None:
        artifact_path(self.database.path.parent, key)
        with self.database.engine.connect() as connection:
            row = (
                connection.execute(
                    select(objects).join(artifact_bindings).where(artifact_bindings.c.key == key)
                )
                .mappings()
                .first()
            )
        return ObjectRef.model_validate(dict(row)) if row else None

    @stored
    def bind(self, key: str, reference: ObjectRef, *, kind: str = "artifact") -> None:
        artifact_path(self.database.path.parent, key)
        self.objects.verify(reference)
        with self.database.transaction() as connection:
            previous = connection.execute(
                select(artifact_bindings.c.sha256).where(artifact_bindings.c.key == key)
            ).scalar_one_or_none()
            if previous is not None and previous != reference.sha256:
                raise KomaquillError(code="operation_conflict", stage="storage")
            connection.execute(
                insert(objects).values(**reference.model_dump()).on_conflict_do_nothing()
            )
            connection.execute(
                insert(artifact_bindings)
                .values(key=key, sha256=reference.sha256, kind=kind)
                .on_conflict_do_nothing()
            )
            connection.commit()

    @stored
    def calls(self, operation: UUID) -> list[tuple[str, int, dict[str, Any]]]:
        with self.database.engine.connect() as connection:
            latest = (
                select(
                    llm_records.c.purpose,
                    llm_records.c.attempt,
                    func.max(llm_records.c.revision).label("latest"),
                )
                .where(llm_records.c.operation_id == str(operation))
                .group_by(llm_records.c.purpose, llm_records.c.attempt)
                .subquery()
            )
            rows = (
                connection.execute(
                    select(
                        llm_records.c.purpose,
                        llm_records.c.attempt,
                        objects.c.sha256,
                        objects.c.size_bytes,
                    )
                    .join(objects)
                    .join(
                        latest,
                        (llm_records.c.purpose == latest.c.purpose)
                        & (llm_records.c.attempt == latest.c.attempt)
                        & (llm_records.c.revision == latest.c.latest),
                    )
                    .where(llm_records.c.operation_id == str(operation))
                    .order_by(llm_records.c.purpose, llm_records.c.attempt)
                )
                .mappings()
                .all()
            )
        return [
            (
                row["purpose"],
                row["attempt"],
                json.loads(
                    self.objects.read(ObjectRef(sha256=row["sha256"], size_bytes=row["size_bytes"]))
                ),
            )
            for row in rows
        ]

    @stored
    def record(self, operation: UUID, purpose: str, attempt: int, payload: dict[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        reference = self.objects.publish(content)
        with self.database.transaction() as connection:
            condition = (
                (llm_records.c.operation_id == str(operation))
                & (llm_records.c.purpose == purpose)
                & (llm_records.c.attempt == attempt)
            )
            previous = (
                connection.execute(
                    select(llm_records)
                    .where(condition)
                    .order_by(llm_records.c.revision.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if previous and previous["sha256"] == reference.sha256:
                return
            # A recorded response cannot be replaced by another response or an earlier state.
            if previous and previous["status"] == "response_received":
                prior = json.loads(
                    self.objects.path(
                        ObjectRef(
                            sha256=previous["sha256"],
                            size_bytes=connection.execute(
                                select(objects.c.size_bytes).where(
                                    objects.c.sha256 == previous["sha256"]
                                )
                            ).scalar_one(),
                        )
                    ).read_bytes()
                )
                if payload.get("status") != "response_received" or any(
                    payload.get(name) != prior.get(name)
                    for name in ("response", "messages", "invocation_id", "usage")
                ):
                    raise KomaquillError(code="operation_conflict", stage="storage")
            connection.execute(
                insert(objects).values(**reference.model_dump()).on_conflict_do_nothing()
            )
            connection.execute(
                llm_records.insert().values(
                    operation_id=str(operation),
                    purpose=purpose,
                    attempt=attempt,
                    revision=previous["revision"] + 1 if previous else 0,
                    sha256=reference.sha256,
                    status=payload.get(
                        "status",
                        "fixture_response" if payload.get("provider") == "fake" else "unknown",
                    ),
                )
            )
            connection.commit()
