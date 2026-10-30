"""Commit new image metadata in SQLite while retaining binary artifact placement."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from komaquill.adapters.database import database, image_submissions, images
from komaquill.adapters.diagnostics import record_event
from komaquill.adapters.paths import WorkspacePaths
from komaquill.adapters.storage_errors import stored
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import Run


@stored
def load(project: WorkspacePaths, unreadable: list[UUID | str] | None = None) -> list[Run]:
    storage = database(project.data / "komaquill.sqlite3")
    with storage.engine.connect() as connection:
        rows = connection.execute(select(images)).mappings().all()
    result = []
    for row in rows:
        try:
            run = Run.model_validate_json(row["payload"])
            if str(run.run_id) != row["id"]:
                raise ValueError("Image identity mismatch.")
            result.append(run)
        except ValueError:
            if unreadable is None:
                raise KomaquillError(code="storage_failed", stage="storage") from None
            try:
                identity: UUID | str = UUID(row["id"])
            except ValueError:
                identity = row["id"]
            unreadable.append(identity)
    return result


def read(project: WorkspacePaths, identity: UUID) -> Run:
    storage = database(project.data / "komaquill.sqlite3")
    with storage.engine.connect() as connection:
        payload = connection.execute(
            select(images.c.payload).where(images.c.id == str(identity))
        ).scalar_one_or_none()
    if payload is None:
        raise FileNotFoundError
    run = Run.model_validate_json(payload)
    if run.run_id != identity:
        raise KomaquillError(code="identity_mismatch", stage="storage")
    return run


@stored
def save(project: WorkspacePaths, run: Run) -> None:
    storage = database(project.data / "komaquill.sqlite3")
    with storage.transaction() as connection:
        previous = connection.execute(
            select(images.c.payload).where(images.c.id == str(run.run_id))
        ).scalar_one_or_none()
        prior = None
        if previous is not None:
            prior = Run.model_validate_json(previous)
            if prior == run:
                return
            if prior.state == "saved" and run.state != "saved":
                raise KomaquillError(code="operation_conflict", stage="storage")
        run.updated_at = datetime.now(UTC)
        if run.state == "saved" and run.saved_at is None:
            run.saved_at = run.updated_at
        if previous is None:
            connection.execute(
                images.insert().values(id=str(run.run_id), payload=run.model_dump_json())
            )
        else:
            connection.execute(
                images.update()
                .where(images.c.id == str(run.run_id))
                .values(payload=run.model_dump_json())
            )
        connection.commit()
    if run.first_candidate_available_at and (
        prior is None or prior.first_candidate_available_at is None
    ):
        record_event(
            project, "image_availability", "available", runtime_id=run.runtime_id, run_id=run.run_id
        )
    if prior is None or prior.state != run.state or prior.failure != run.failure:
        record_event(
            project,
            "generation",
            run.state,
            runtime_id=run.runtime_id,
            run_id=run.run_id,
            failure=run.failure,
        )
    if (
        run.collection_failure is not None or (prior and prior.collection_failure is not None)
    ) and (prior is None or prior.collection_failure != run.collection_failure):
        record_event(
            project,
            "image_collection",
            "failed" if run.collection_failure else "recovered",
            runtime_id=run.runtime_id,
            run_id=run.run_id,
            failure=run.collection_failure,
        )


@stored
def reserve(project: WorkspacePaths, identity: UUID, fingerprint: str) -> bool:
    storage = database(project.data / "komaquill.sqlite3")
    with storage.transaction() as connection:
        previous = connection.execute(
            select(image_submissions.c.fingerprint).where(image_submissions.c.id == str(identity))
        ).scalar_one_or_none()
        if previous is not None:
            if previous != fingerprint:
                raise KomaquillError(code="operation_conflict", stage="submission")
            return False
        connection.execute(
            image_submissions.insert().values(id=str(identity), fingerprint=fingerprint)
        )
        connection.commit()
        return True


@stored
def cancel_unsubmitted(project: WorkspacePaths, identity: UUID) -> None:
    """Release a failed preflight only when no external submission checkpoint exists."""
    storage = database(project.data / "komaquill.sqlite3")
    with storage.transaction() as connection:
        checkpoint = connection.execute(
            select(images.c.id).where(images.c.id == str(identity))
        ).first()
        if checkpoint is None:
            connection.execute(
                image_submissions.delete().where(image_submissions.c.id == str(identity))
            )
        connection.commit()
