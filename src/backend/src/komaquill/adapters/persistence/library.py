"""Persist typed presets and short creation checkpoints in the shared database."""

import builtins
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from uuid import UUID

import psutil
from sqlalchemy import case, func, select
from sqlalchemy.engine import Connection

from komaquill.adapters.persistence.connection import Database
from komaquill.adapters.persistence.errors import conflict, missing
from komaquill.adapters.persistence.schema import (
    conversations,
    creation_jobs,
    preferences,
    preset_saves,
    presets,
)
from komaquill.adapters.storage_errors import stored
from komaquill.domain.conversations.models import Conversation
from komaquill.domain.errors import KomaquillError
from komaquill.domain.library.models import (
    CharacterPreset,
    CreationJob,
    CreationRequest,
    PlayerProfile,
    Preferences,
    Preset,
    PresetInput,
    PresetKind,
    PresetSaveOptions,
    PresetSaveResult,
    StoryInput,
    StoryPreset,
)
from komaquill.domain.library.rendering import preset_changes, preset_image_input

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
            payload = case(
                (func.json_valid(creation_jobs.c.payload) == 1, creation_jobs.c.payload),
                else_="{}",
            )
            rows = connection.execute(
                select(creation_jobs.c.id, creation_jobs.c.payload).where(
                    func.json_extract(payload, "$.request.kind") == "conversation_image",
                    func.json_extract(payload, "$.request.target_id") == str(identity),
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
        self,
        kind: PresetKind,
        value: PresetInput,
        *,
        image_id: UUID | None = None,
        image_stale: bool = False,
    ) -> Preset:
        result = MODELS[kind].model_validate(
            {**value.model_dump(), "image_id": image_id, "image_stale": image_stale}
        )
        with self.database.write() as connection:
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
                    "image_stale": bool(previous.image_id)
                    if preset_image_input(kind, previous) != preset_image_input(kind, value)
                    else previous.image_stale,
                    "image_job_id": None
                    if preset_image_input(kind, previous) != preset_image_input(kind, value)
                    else previous.image_job_id,
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
                    "image_stale": False if image else value.image_stale,
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

    def _accept(
        self,
        connection: Connection,
        request: CreationRequest,
        original: str,
        turn_id: UUID | None = None,
    ) -> tuple[CreationJob, bool]:
        row = connection.execute(
            select(creation_jobs.c.payload).where(creation_jobs.c.id == str(request.operation_id))
        ).scalar_one_or_none()
        if row is not None:
            job = self._decode_job(str(request.operation_id), row)
            if job.request != request:
                raise conflict()
            return job, False
        active = case(
            (
                func.json_valid(creation_jobs.c.payload) == 1,
                func.json_extract(creation_jobs.c.payload, "$.state"),
            ),
            else_=None,
        )
        target = case(
            (
                func.json_valid(creation_jobs.c.payload) == 1,
                func.json_extract(creation_jobs.c.payload, "$.request.target_id"),
            ),
            else_=None,
        )
        purpose = case(
            (
                func.json_valid(creation_jobs.c.payload) == 1,
                func.json_extract(creation_jobs.c.payload, "$.request.kind"),
            ),
            else_=None,
        )
        version = case(
            (
                func.json_valid(creation_jobs.c.payload) == 1,
                func.json_extract(creation_jobs.c.payload, "$.request.expected_revision"),
            ),
            else_=None,
        )
        if (
            request.target_id is not None
            and connection.execute(
                select(creation_jobs.c.id).where(
                    target == str(request.target_id),
                    purpose == request.kind,
                    version == request.expected_revision,
                    active.in_(["accepted", "running", "submitted"]),
                )
            ).first()
            is not None
        ):
            raise KomaquillError(code="operation_busy", retryable=True)
        value = None
        if request.kind == "conversation_image":
            payload = connection.execute(
                select(conversations.c.payload).where(conversations.c.id == str(request.target_id))
            ).scalar_one_or_none()
            if payload is None:
                raise missing()
            scene = Conversation.model_validate_json(payload)
            if scene.revision != request.expected_revision:
                raise conflict()
        if request.kind in {"portrait", "banner"}:
            kind: PresetKind = "character" if request.kind == "portrait" else "story"
            payload = connection.execute(
                select(presets.c.payload).where(
                    presets.c.id == str(request.target_id),
                    presets.c.kind == kind,
                    presets.c.deleted.is_(False),
                )
            ).scalar_one_or_none()
            if payload is None:
                raise missing()
            value = self._decode(kind, str(request.target_id), payload)
            if value.revision != request.expected_revision:
                raise conflict()
        job = CreationJob(
            id=request.operation_id,
            request=request,
            original=original,
            state="accepted",
            turn_id=turn_id,
        )
        enabled = self.database.content.capture(request.operation_id, connection)
        job = job.model_copy(update={"nsfw_enabled": enabled})
        connection.execute(
            creation_jobs.insert().values(
                id=str(job.id),
                payload=job.model_dump_json(),
                owner_pid=os.getpid(),
                owner_started=str(psutil.Process().create_time()),
            )
        )
        if value is not None:
            updated = value.model_copy(update={"image_job_id": job.id})
            connection.execute(
                presets.update()
                .where(presets.c.id == str(value.id))
                .values(payload=updated.model_dump_json())
            )
        return job, True

    @stored
    def accept(
        self, request: CreationRequest, original: str, *, turn_id: UUID | None = None
    ) -> tuple[CreationJob, bool]:
        with self.database.transaction() as connection:
            result = self._accept(connection, request, original, turn_id)
            connection.commit()
            return result

    @stored
    def save_preset(
        self,
        kind: PresetKind,
        value: PresetInput,
        options: PresetSaveOptions,
    ) -> PresetSaveResult:
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "kind": kind,
                    "value": value.model_dump(mode="json"),
                    "options": options.model_dump(mode="json"),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.database.transaction() as connection:
            previous_receipt = (
                connection.execute(
                    select(preset_saves).where(preset_saves.c.id == str(options.operation_id))
                )
                .mappings()
                .one_or_none()
            )
            if previous_receipt is not None:
                if previous_receipt["fingerprint"] != fingerprint:
                    raise conflict()
                return PresetSaveResult.model_validate_json(previous_receipt["payload"])
            previous = None
            if options.target_id is not None:
                payload = connection.execute(
                    select(presets.c.payload).where(
                        presets.c.id == str(options.target_id),
                        presets.c.kind == kind,
                        presets.c.deleted.is_(False),
                    )
                ).scalar_one_or_none()
                if payload is None:
                    raise missing()
                previous = self._decode(kind, str(options.target_id), payload)
                if previous.revision != options.expected_revision:
                    raise conflict()
            elif options.expected_revision is not None:
                raise conflict()
            if (
                kind == "story"
                and isinstance(value, StoryInput)
                and value.character_id is not None
                and connection.execute(
                    select(presets.c.id).where(
                        presets.c.id == str(value.character_id),
                        presets.c.kind == "character",
                        presets.c.deleted.is_(False),
                    )
                ).first()
                is None
            ):
                raise missing()
            changes = preset_changes(kind, value, previous)
            record = MODELS[kind].model_validate(
                {
                    **(previous.model_dump() if previous else {}),
                    **value.model_dump(),
                    **(
                        {
                            "revision": previous.revision + 1,
                            "sample": False,
                            "sample_locale": None,
                            "image_job_id": None
                            if changes.image_changed
                            else previous.image_job_id,
                            "image_stale": bool(previous.image_id)
                            if changes.image_changed
                            else previous.image_stale,
                        }
                        if previous and changes.content_changed
                        else {}
                    ),
                }
            )
            if previous is None:
                connection.execute(
                    presets.insert().values(
                        id=str(record.id),
                        kind=kind,
                        payload=record.model_dump_json(),
                        deleted=False,
                    )
                )
            elif changes.content_changed:
                connection.execute(
                    presets.update()
                    .where(presets.c.id == str(record.id))
                    .values(payload=record.model_dump_json())
                )
            result = PresetSaveResult(preset=cast(CharacterPreset | StoryPreset, record))
            connection.execute(
                preset_saves.insert().values(
                    id=str(options.operation_id),
                    fingerprint=fingerprint,
                    payload=result.model_dump_json(),
                )
            )
            connection.commit()
            return result

    @stored
    def job_ids(self) -> builtins.list[UUID]:
        with self.database.engine.connect() as connection:
            return [
                UUID(row.id)
                for row in connection.execute(
                    select(creation_jobs.c.id).where(
                        case(
                            (
                                func.json_valid(creation_jobs.c.payload) == 1,
                                func.json_extract(creation_jobs.c.payload, "$.state"),
                            ),
                            else_="invalid",
                        ).in_(["accepted", "running", "submitted", "invalid"])
                    )
                )
            ]

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
            else:
                if value.state == "running":
                    with self.database.execution_lock(identity) as available:
                        alive = not available
        if not alive and value.state == "running":
            retained = self.database.checkpoints.load(f"creation:{identity}")
            if retained is not None:
                saved = self._decode_job(str(identity), json.dumps(retained))
                if saved.state == "complete" and saved.request.kind.endswith("_draft"):
                    value = self.save_job(saved)
        return value, alive

    @contextmanager
    def execution(self, identity: UUID) -> Iterator[CreationJob | None]:
        with self.database.execution_lock(identity) as acquired:
            yield self.claim(identity) if acquired else None

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
            request = value.request
            if value.state == "complete" and request.kind in {"portrait", "banner"}:
                target_payload = connection.execute(
                    select(presets.c.payload).where(
                        presets.c.id == str(request.target_id), presets.c.deleted.is_(False)
                    )
                ).scalar_one_or_none()
                if target_payload is not None:
                    kind: PresetKind = "character" if request.kind == "portrait" else "story"
                    target = self._decode(kind, str(request.target_id), target_payload)
                    if (
                        target.revision == request.expected_revision
                        or preset_image_input(kind, target) == json.loads(value.original)
                    ) and target.image_job_id == value.id:
                        target = target.model_copy(
                            update={"image_id": value.image_id, "image_stale": False}
                        )
                        connection.execute(
                            presets.update()
                            .where(presets.c.id == str(target.id))
                            .values(payload=target.model_dump_json())
                        )
            connection.execute(
                creation_jobs.update()
                .where(creation_jobs.c.id == str(value.id))
                .values(payload=value.model_dump_json())
            )
            connection.commit()
            return value

    @stored
    def retain_job(self, value: CreationJob) -> None:
        self._decode_job(str(value.id), value.model_dump_json())
        self.database.checkpoints.save(f"creation:{value.id}", value.model_dump(mode="json"))

    def _decode_job(self, identity: str, payload: str) -> CreationJob:
        value = CreationJob.model_validate_json(payload)
        if (
            str(value.id) != identity
            or str(value.request.operation_id) != identity
            or (value.image_id is not None and str(value.image_id) != identity)
        ):
            raise ValueError("Creation identity mismatch.")
        return value
