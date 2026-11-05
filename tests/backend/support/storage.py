"""Locate canonical objects when checking physical corruption and preservation."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import UUID

from komaquill.adapters.artifacts import LocalArtifactStore
from komaquill.adapters.paths import WorkspacePaths
from komaquill.adapters.persistence.artifacts import ArtifactRepository
from komaquill.adapters.persistence.connection import database
from komaquill.adapters.persistence.schema import llm_records, objects
from komaquill.domain.artifacts import ObjectRef
from sqlalchemy import select


def artifact(project: WorkspacePaths, identity: UUID, name: str) -> Path:
    return LocalArtifactStore(project).path(f"runs/{identity}/{name}")


def call_file(workspace: Path, operation: UUID, attempt: int = 0, purpose: str = "") -> Path:
    repository = ArtifactRepository(database(workspace / ".artifacts/data/komaquill.sqlite3"))
    with repository.database.engine.connect() as connection:
        row = (
            connection.execute(
                select(objects)
                .join(llm_records)
                .where(
                    llm_records.c.operation_id == str(operation),
                    llm_records.c.attempt == attempt,
                    llm_records.c.purpose == purpose,
                )
                .order_by(llm_records.c.revision.desc())
                .limit(1)
            )
            .mappings()
            .one()
        )
    return repository.objects.path(ObjectRef.model_validate(dict(row)))


def call_files(workspace: Path, operation: UUID) -> list[Path]:
    repository = ArtifactRepository(database(workspace / ".artifacts/data/komaquill.sqlite3"))
    return [
        call_file(workspace, operation, attempt, purpose)
        for purpose, attempt, _ in repository.calls(operation)
    ]


@contextmanager
def sqlite_connection(*args, **kwargs):
    """Commit or roll back and always release the SQLite file handle."""
    with closing(sqlite3.connect(*args, **kwargs)) as connection, connection:
        yield connection
