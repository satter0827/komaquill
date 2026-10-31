"""Persist typed presets and short creation checkpoints in the shared database."""

import os
from typing import cast
from uuid import UUID

import psutil
from sqlalchemy import Boolean, Column, Integer, String, Table, Text, select

from komaquill.adapters.database import Database, conflict, metadata, missing
from komaquill.adapters.storage_errors import stored
from komaquill.library.models import (
    CharacterPreset,
    CreationJob,
    CreationRequest,
    PlayerProfile,
    Preferences,
    Preset,
    PresetInput,
    PresetKind,
    StoryPreset,
)

presets = Table(
    "presets",
    metadata,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("deleted", Boolean, nullable=False, default=False),
)
preferences = Table(
    "preferences",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("payload", Text, nullable=False),
)
creation_jobs = Table(
    "creation_jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("owner_pid", Integer, nullable=False),
    Column("owner_started", String, nullable=False),
)
MODELS: dict[PresetKind, type[PlayerProfile] | type[CharacterPreset] | type[StoryPreset]] = {
    "player": PlayerProfile,
    "character": CharacterPreset,
    "story": StoryPreset,
}


class LibraryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @stored
    def conversation_images(self, identity: UUID) -> list[CreationJob]:
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(creation_jobs.c.id, creation_jobs.c.payload).where(
                    creation_jobs.c.payload.op("->>")("$.request.kind") == "conversation_image",
                    creation_jobs.c.payload.op("->>")("$.request.target_id") == str(identity),
                )
            )
            return sorted(
                (self._decode_job(row.id, row.payload) for row in rows),
                key=lambda job: job.created_at,
            )

    @stored
    def initialize(self, values: list[tuple[PresetKind, Preset]]) -> None:
        with self.database.transaction() as connection:
            if connection.execute(select(preferences.c.id)).first() is not None:
                return
            for kind, value in values:
                connection.execute(
                    presets.insert().values(
                        id=str(value.id),
                        kind=kind,
                        payload=value.model_dump_json(),
                        deleted=False,
                    )
                )
            connection.execute(
                preferences.insert().values(
                    id=1,
                    payload=Preferences().model_dump_json(),
                )
            )
            connection.commit()

    @stored
    def list(self, kind: PresetKind) -> list[Preset]:
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(presets).where(
                    presets.c.kind == kind,
                    presets.c.deleted == False,  # noqa: E712
                )
            ).mappings()
            return [self._decode(kind, row["id"], row["payload"]) for row in rows]

    def _decode(self, kind: PresetKind, identity: str, payload: str) -> Preset:
        value = MODELS[kind].model_validate_json(payload)
        if str(value.id) != identity:
            raise ValueError("Preset identity mismatch.")
        return value

    @stored
    def get(self, kind: PresetKind, identity: UUID) -> Preset:
        with self.database.engine.connect() as connection:
            row = (
                connection.execute(
                    select(presets).where(
                        presets.c.kind == kind,
                        presets.c.id == str(identity),
                        presets.c.deleted == False,  # noqa: E712
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise missing()
            return self._decode(kind, row["id"], row["payload"])

    @stored
    def resolve(self, references: dict[PresetKind, tuple[UUID, int]]) -> dict[PresetKind, Preset]:
        with self.database.transaction() as connection:
            result: dict[PresetKind, Preset] = {}
            for kind, (identity, revision) in references.items():
                payload = connection.execute(
                    select(presets.c.payload).where(
                        presets.c.id == str(identity),
                        presets.c.kind == kind,
                        presets.c.deleted == False,  # noqa: E712
                    )
                ).scalar_one_or_none()
                if payload is None:
                    raise missing()
                value = self._decode(kind, str(identity), payload)
                if value.revision != revision:
                    raise conflict()
                result[kind] = value
            return result

    @stored
    def create(
        self, kind: PresetKind, value: PresetInput, *, image_id: UUID | None = None
    ) -> Preset:
        result = MODELS[kind].model_validate({**value.model_dump(), "image_id": image_id})
        with self.database.engine.begin() as connection:
            connection.execute(
                presets.insert().values(
                    id=str(result.id), kind=kind, payload=result.model_dump_json(), deleted=False
                )
            )
        return result

    @stored
    def update(self, kind: PresetKind, identity: UUID, revision: int, value: PresetInput) -> Preset:
        with self.database.transaction() as connection:
            row = (
                connection.execute(
                    select(presets).where(
                        presets.c.id == str(identity),
                        presets.c.kind == kind,
                        presets.c.deleted == False,  # noqa: E712
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise missing()
            previous = self._decode(kind, str(identity), row["payload"])
            if previous.revision != revision:
                raise conflict()
            result = MODELS[kind].model_validate(
                {
                    **previous.model_dump(),
                    **value.model_dump(),
                    "revision": revision + 1,
                    "sample": False,
                    "sample_locale": None,
                    "image_job_id": None,
                }
            )
            connection.execute(
                presets.update()
                .where(presets.c.id == str(identity))
                .values(payload=result.model_dump_json())
            )
            connection.commit()
            return result

    @stored
    def delete(self, kind: PresetKind, identity: UUID, revision: int) -> None:
        with self.database.transaction() as connection:
            row = (
                connection.execute(
                    select(presets).where(
                        presets.c.id == str(identity),
                        presets.c.kind == kind,
                        presets.c.deleted == False,  # noqa: E712
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise missing()
            if self._decode(kind, str(identity), row["payload"]).revision != revision:
                raise conflict()
            connection.execute(
                presets.update().where(presets.c.id == str(identity)).values(deleted=True)
            )
            if kind == "character":
                rows = (
                    connection.execute(
                        select(presets).where(
                            presets.c.kind == "story", presets.c.deleted.is_(False)
                        )
                    )
                    .mappings()
                    .all()
                )  # noqa: E712
                for row in rows:
                    story = cast(StoryPreset, self._decode("story", row["id"], row["payload"]))
                    if story.character_id == identity:
                        changed = story.model_copy(
                            update={
                                "character_id": None,
                                "revision": story.revision + 1,
                                "image_job_id": None,
                            }
                        )
                        connection.execute(
                            presets.update()
                            .where(presets.c.id == row["id"])
                            .values(payload=changed.model_dump_json())
                        )
            connection.commit()

    @stored
    def attach(
        self, kind: PresetKind, identity: UUID, revision: int, job: UUID, image: UUID | None = None
    ) -> bool:
        with self.database.transaction() as connection:
            row = (
                connection.execute(
                    select(presets).where(
                        presets.c.id == str(identity),
                        presets.c.kind == kind,
                        presets.c.deleted == False,  # noqa: E712
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return False
            value = self._decode(kind, str(identity), row["payload"])
            if value.revision != revision or (image and value.image_job_id != job):
                return False
            updated = value.model_copy(
                update={
                    "image_job_id": job,
                    "image_id": image or value.image_id,
                }
            )
            connection.execute(
                presets.update()
                .where(presets.c.id == str(identity))
                .values(payload=updated.model_dump_json())
            )
            connection.commit()
            return True

    @stored
    def preferences(self) -> Preferences:
        with self.database.engine.connect() as connection:
            payload = connection.execute(
                select(preferences.c.payload).where(preferences.c.id == 1)
            ).scalar_one_or_none()
            return Preferences() if payload is None else Preferences.model_validate_json(payload)

    @stored
    def save_preferences(self, value: Preferences) -> Preferences:
        with self.database.transaction() as connection:
            payload = connection.execute(
                select(preferences.c.payload).where(preferences.c.id == 1)
            ).scalar_one_or_none()
            previous = (
                Preferences() if payload is None else Preferences.model_validate_json(payload)
            )
            if previous.revision != value.revision:
                raise conflict()
            result = value.model_copy(update={"revision": value.revision + 1})
            if payload is None:
                connection.execute(
                    preferences.insert().values(id=1, payload=result.model_dump_json())
                )
            else:
                connection.execute(
                    preferences.update()
                    .where(preferences.c.id == 1)
                    .values(payload=result.model_dump_json())
                )
            connection.commit()
            return result

    @stored
    def accept(
        self, request: CreationRequest, original: str, *, turn_id: UUID | None = None
    ) -> tuple[CreationJob, bool]:
        with self.database.transaction() as connection:
            row = connection.execute(
                select(creation_jobs.c.payload).where(
                    creation_jobs.c.id == str(request.operation_id)
                )
            ).scalar_one_or_none()
            if row is not None:
                job = self._decode_job(str(request.operation_id), row)
                if job.request != request:
                    raise conflict()
                return job, False
            job = CreationJob(
                id=request.operation_id,
                request=request,
                original=original,
                state="accepted",
                turn_id=turn_id,
            )
            connection.execute(
                creation_jobs.insert().values(
                    id=str(job.id),
                    payload=job.model_dump_json(),
                    owner_pid=os.getpid(),
                    owner_started=str(psutil.Process().create_time()),
                )
            )
            connection.commit()
            return job, True

    @stored
    def job(self, identity: UUID) -> tuple[CreationJob, bool]:
        with self.database.engine.connect() as connection:
            row = (
                connection.execute(select(creation_jobs).where(creation_jobs.c.id == str(identity)))
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise missing()
            value = self._decode_job(str(identity), row["payload"])
            try:
                alive = str(psutil.Process(row["owner_pid"]).create_time()) == row["owner_started"]
            except psutil.NoSuchProcess:
                alive = False
            except psutil.Error:
                alive = True
            return value, alive

    @stored
    def claim(self, identity: UUID) -> CreationJob | None:
        with self.database.transaction() as connection:
            payload = connection.execute(
                select(creation_jobs.c.payload).where(
                    creation_jobs.c.id == str(identity),
                    creation_jobs.c.owner_pid == os.getpid(),
                    creation_jobs.c.owner_started == str(psutil.Process().create_time()),
                )
            ).scalar_one_or_none()
            if payload is None:
                return None
            value = self._decode_job(str(identity), payload)
            if value.state != "accepted":
                return None
            value = value.model_copy(update={"state": "running"})
            connection.execute(
                creation_jobs.update()
                .where(creation_jobs.c.id == str(identity))
                .values(payload=value.model_dump_json())
            )
            connection.commit()
            return value

    @stored
    def save_job(self, value: CreationJob) -> CreationJob:
        with self.database.transaction() as connection:
            payload: str = connection.execute(
                select(creation_jobs.c.payload).where(creation_jobs.c.id == str(value.id))
            ).scalar_one()
            previous = self._decode_job(str(value.id), payload)
            self._decode_job(str(value.id), value.model_dump_json())
            if any(
                getattr(previous, key) != getattr(value, key)
                for key in ("request", "original", "turn_id", "created_at")
            ) or (
                previous.prompt_en is not None
                and value.prompt_en is not None
                and previous.prompt_en != value.prompt_en
            ):
                raise conflict()
            ranks = {
                "accepted": 0,
                "running": 1,
                "submitted": 2,
                "complete": 3,
                "failed": 3,
                "interrupted": 3,
            }
            if ranks[previous.state] > ranks[value.state] or (
                ranks[previous.state] == 3 and previous != value
            ):
                return previous
            if previous == value:
                return previous
            value = value.model_copy(
                update={
                    key: getattr(previous, key)
                    for key in ("prompt_en", "image_id", "result")
                    if getattr(previous, key) is not None and getattr(value, key) is None
                }
            )
            connection.execute(
                creation_jobs.update()
                .where(creation_jobs.c.id == str(value.id))
                .values(payload=value.model_dump_json())
            )
            connection.commit()
            return value

    def _decode_job(self, identity: str, payload: str) -> CreationJob:
        value = CreationJob.model_validate_json(payload)
        if (
            str(value.id) != identity
            or str(value.request.operation_id) != identity
            or (value.image_id is not None and str(value.image_id) != identity)
        ):
            raise ValueError("Creation identity mismatch.")
        return value
