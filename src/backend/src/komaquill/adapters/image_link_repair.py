"""Inspect and repair stopped-workspace image links using retained requests and bytes."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from filelock import FileLock, Timeout
from PIL import Image
from pydantic import Field

from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.paths import artifact_path
from komaquill.adapters.persistence.migrate import database_in_use, inventory
from komaquill.adapters.storage_errors import stored
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.domain.errors import KomaquillError
from komaquill.domain.library.models import CharacterPreset, CreationJob, StoryPreset
from komaquill.domain.library.recovery import (
    ImageLinkFinding,
    ImageLinkInspection,
    ImageLinkRepairResult,
    RepairReason,
    matches_image_input,
)
from komaquill.domain.models import Run
from komaquill.domain.roleplay import Record


class Change(Record):
    kind: Literal["character", "story"]
    target: str
    previous_job: str
    job: str
    files: dict[str, str]


class Plan(Record):
    format_version: Literal[1] = 1
    inspection: ImageLinkInspection
    changes: list[Change] = Field(default_factory=list)


def _connect(project: RuntimeWorkspace, *, writable: bool = False) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    return sqlite3.connect(
        (project.data / "komaquill.sqlite3").resolve().as_uri() + f"?mode={mode}", uri=True
    )


def _folder(project: RuntimeWorkspace, identity: UUID) -> Path:
    return artifact_path(project.records, f"image-link-repairs/{identity}")


def _image_visible(connection: sqlite3.Connection, identity: UUID) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM work_metadata "
            "WHERE kind='image' AND id=? AND output_index=0 AND trashed=1",
            (str(identity),),
        ).fetchone()
        is None
    )


def _files(
    project: RuntimeWorkspace, connection: sqlite3.Connection, job: CreationJob
) -> dict[str, str]:
    payload = connection.execute("SELECT payload FROM images WHERE id=?", (str(job.id),)).fetchone()
    if payload is None:
        return {}
    run = Run.model_validate_json(payload[0])
    if (
        run.run_id != job.id
        or run.state != "saved"
        or not run.images
        or job.image_id not in {None, job.id}
    ):
        return {}
    result = {}
    for name in run.images:
        try:
            path = project.artifacts_store.path(f"runs/{run.run_id}/{name}")
        except FileNotFoundError:
            return {}
        except KomaquillError as error:
            if error.code == "identity_mismatch":
                return {}
            raise
        data = path.read_bytes()
        metadata = run.image_metadata.get(name)
        digest = hashlib.sha256(data).hexdigest()
        if metadata is None or digest != metadata.sha256 or len(data) != metadata.size_bytes:
            return {}
        with Image.open(path) as image:
            if image.size != (metadata.width, metadata.height):
                return {}
            image.verify()
        result[path.relative_to(project.root).as_posix()] = digest
    return result


def _inspect(project: RuntimeWorkspace, connection: sqlite3.Connection) -> Plan:
    findings, changes = [], []
    jobs = dict(connection.execute("SELECT id,payload FROM creation_jobs"))
    for identity, kind, payload in connection.execute(
        "SELECT id,kind,payload FROM presets WHERE deleted=0 AND kind IN ('character','story')"
    ):
        target = (CharacterPreset if kind == "character" else StoryPreset).model_validate_json(
            payload
        )
        if str(target.id) != identity:
            raise KomaquillError(code="storage_failed", stage="storage", retryable=False)
        previous = jobs.get(str(target.image_job_id))
        if target.image_id is not None or previous is None:
            continue
        failed = CreationJob.model_validate_json(previous)
        if failed.state != "failed" or failed.error != "operation_busy":
            continue
        reason: RepairReason = "no_candidate"
        candidates = []
        files = {}
        if (
            failed.id != target.image_job_id
            or failed.request.operation_id != failed.id
            or not matches_image_input(kind, target, failed)
        ):
            reason = "input_changed"
        else:
            for key, raw in jobs.items():
                job = CreationJob.model_validate_json(raw)
                if str(job.id) != key or job.request.operation_id != job.id:
                    raise KomaquillError(code="storage_failed", stage="storage", retryable=False)
                if job.id == failed.id or job.state not in {"submitted", "complete"}:
                    continue
                if _image_visible(connection, job.id) and matches_image_input(kind, target, job):
                    candidates.append(job.id)
            if len(candidates) > 1:
                reason = "ambiguous"
            elif candidates:
                job = CreationJob.model_validate_json(jobs[str(candidates[0])])
                try:
                    files = _files(project, connection, job)
                except (OSError, ValueError):
                    files = {}
                reason = "ready" if files else "invalid_image"
                if files:
                    changes.append(
                        Change(
                            kind=kind,
                            target=payload,
                            previous_job=previous,
                            job=jobs[str(job.id)],
                            files=files,
                        )
                    )
        findings.append(
            ImageLinkFinding(target_id=target.id, kind=kind, candidates=candidates, reason=reason)
        )
    return Plan(inspection=ImageLinkInspection(id=uuid4(), findings=findings), changes=changes)


@stored
def inspect(workspace: Path) -> ImageLinkInspection:
    project = RuntimeWorkspace(workspace)
    with closing(_connect(project)) as connection:
        connection.execute("BEGIN")
        plan = _inspect(project, connection)
    atomic_json(
        _folder(project, plan.inspection.id) / "inspection.json", plan.model_dump(mode="json")
    )
    return plan.inspection


@stored
def repair(workspace: Path, identity: UUID) -> ImageLinkRepairResult:
    project = RuntimeWorkspace(workspace)
    project.state.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(project.state / ".image-link-repair.lock"), timeout=0):
            return _repair(project, identity)
    except Timeout:
        raise KomaquillError(code="operation_busy", stage="storage", retryable=False) from None


def _backup(project: RuntimeWorkspace, folder: Path) -> None:
    backup, manifest = folder / "before.sqlite3", folder / "backup.json"
    with closing(_connect(project)) as connection:
        before = inventory(connection)
        if not backup.exists():
            with closing(sqlite3.connect(backup)) as copied:
                connection.backup(copied)
        with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)) as copied:
            saved = inventory(copied)
            if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise KomaquillError(code="storage_failed", stage="storage", retryable=False)
        digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        if manifest.exists():
            record = json.loads(manifest.read_bytes())
            if record != {"before": saved, "sha256": digest}:
                raise KomaquillError(code="storage_failed", stage="storage", retryable=False)
        elif saved != before:
            raise KomaquillError(code="storage_failed", stage="storage", retryable=False)
        else:
            atomic_json(manifest, {"before": saved, "sha256": digest})


def _repair(project: RuntimeWorkspace, identity: UUID) -> ImageLinkRepairResult:
    if database_in_use(project):
        raise KomaquillError(
            code="operation_busy",
            stage="storage",
            retryable=False,
            display_key="storage.repair_stop_required",
        )
    folder = _folder(project, identity)
    plan = Plan.model_validate_json((folder / "inspection.json").read_bytes())
    if plan.inspection.id != identity:
        raise KomaquillError(code="storage_failed", stage="storage", retryable=False)
    _backup(project, folder)
    repaired: list[UUID] = []
    already: list[UUID] = []
    skipped: dict[UUID, str] = {
        finding.target_id: finding.reason
        for finding in plan.inspection.findings
        if finding.reason != "ready"
    }
    with closing(_connect(project, writable=True)) as connection:
        connection.execute("BEGIN")
        refreshed = _inspect(project, connection)
        current = {item.target: item for item in refreshed.changes}
        connection.rollback()
        connection.execute("BEGIN IMMEDIATE")
        for item in plan.changes:
            target = (
                CharacterPreset if item.kind == "character" else StoryPreset
            ).model_validate_json(item.target)
            job = CreationJob.model_validate_json(item.job)
            if not _image_visible(connection, job.id):
                skipped[target.id] = "changed_since_inspection"
                continue
            desired_target = target.model_copy(update={"image_id": job.id, "image_job_id": job.id})
            desired_job = job.model_copy(
                update={"state": "complete", "image_id": job.id, "error": None}
            )
            row = connection.execute(
                "SELECT payload,deleted FROM presets WHERE id=?", (str(target.id),)
            ).fetchone()
            raw_job = connection.execute(
                "SELECT payload FROM creation_jobs WHERE id=?", (str(job.id),)
            ).fetchone()
            if (
                row
                and not row[1]
                and json.loads(row[0]) == desired_target.model_dump(mode="json")
                and raw_job
                and json.loads(raw_job[0]) == desired_job.model_dump(mode="json")
            ):
                already.append(target.id)
                continue
            failed = CreationJob.model_validate_json(item.previous_job)
            raw_failed = connection.execute(
                "SELECT payload FROM creation_jobs WHERE id=?", (str(failed.id),)
            ).fetchone()
            if (
                current.get(item.target) != item
                or row != (item.target, 0)
                or raw_job != (item.job,)
                or raw_failed != (item.previous_job,)
            ):
                skipped[target.id] = "changed_since_inspection"
                continue
            connection.execute(
                "UPDATE creation_jobs SET payload=? WHERE id=?",
                (desired_job.model_dump_json(), str(job.id)),
            )
            connection.execute(
                "UPDATE presets SET payload=? WHERE id=?",
                (desired_target.model_dump_json(), str(target.id)),
            )
            repaired.append(target.id)
        connection.commit()
        after = inventory(connection)
    result = ImageLinkRepairResult(
        id=identity, repaired=repaired, already_applied=already, skipped=skipped
    )
    record = {"result": result.model_dump(mode="json"), "after": after}
    atomic_json(folder / "attempts" / f"{uuid4()}.json", record)
    atomic_json(folder / "result.json", record)
    return result
