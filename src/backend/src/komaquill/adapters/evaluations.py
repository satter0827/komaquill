"""Persist evaluations and export bounded evidence for external AI analysis."""

import hashlib
import io
import json
import os
import sys
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from filelock import FileLock
from PIL import Image, UnidentifiedImageError

from komaquill.adapters.asset_store import inventory
from komaquill.adapters.atomic import atomic_json, discard_temporary
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.domain.errors import KomaquillError
from komaquill.domain.evaluations import Evaluation
from komaquill.domain.models import JsonObject
from komaquill.domain.workflow import digest


class EvaluationRepository:
    """Append records under a single lock without changing original evaluations."""

    def __init__(self, project: RuntimeWorkspace) -> None:
        self.project = project

    def lock(self) -> AbstractContextManager[object]:
        self.project.state.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.project.state / ".evaluations.lock"))

    def load(self, unreadable: list[str] | None = None) -> list[Evaluation]:
        from sqlalchemy import select

        from komaquill.adapters.database import database, evaluations

        storage = database(self.project.data / "komaquill.sqlite3")
        with storage.engine.connect() as connection:
            values = connection.execute(select(evaluations)).mappings().all()
        rows = []
        for value in values:
            try:
                evaluation = Evaluation.model_validate_json(value["payload"])
                if str(evaluation.evaluation_id) != value["id"]:
                    raise ValueError("Evaluation identity mismatch.")
                rows.append(evaluation)
            except ValueError:
                if unreadable is None:
                    raise KomaquillError(code="storage_failed", stage="evaluation") from None
                unreadable.append(value["id"])
        return sorted(rows, key=lambda row: (row.created_at, str(row.evaluation_id)))

    def append(self, evaluation: Evaluation) -> None:
        from sqlalchemy import select

        from komaquill.adapters.database import database, evaluations

        storage = database(self.project.data / "komaquill.sqlite3")
        with storage.transaction() as connection:
            previous = connection.execute(
                select(evaluations.c.payload).where(
                    evaluations.c.id == str(evaluation.evaluation_id)
                )
            ).scalar_one_or_none()
            if previous is not None:
                if Evaluation.model_validate_json(previous) != evaluation:
                    raise KomaquillError(code="invalid_evaluation", stage="evaluation")
                return
            connection.execute(
                evaluations.insert().values(
                    id=str(evaluation.evaluation_id), payload=evaluation.model_dump_json()
                )
            )
            connection.commit()


def read_import(path: Path) -> list[Evaluation]:
    """Accept exported records or a plain list without executing any workflow."""
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        raise KomaquillError(code="invalid_evaluation", stage="evaluation") from None
    if isinstance(value, dict):
        if value.get("format_version") != 1:
            raise KomaquillError(code="invalid_evaluation", stage="evaluation")
        value = value.get("evaluations")
    if not isinstance(value, list):
        raise KomaquillError(code="invalid_evaluation", stage="evaluation")
    return [Evaluation.model_validate(row) for row in value]


def select(
    project: RuntimeWorkspace, run_id: UUID | None = None, *, unreadable: list[str] | None = None
) -> list[Evaluation]:
    """Filter records by either side of a comparison."""
    rows = EvaluationRepository(project).load(unreadable)
    return [row for row in rows if run_id is None or run_id in {row.run_id, row.comparison_run_id}]


def export_evidence(
    project: RuntimeWorkspace, run_id: UUID | None = None, *, run_ids: set[UUID] | None = None
) -> JsonObject:
    """Expose saved conditions and artifact keys without logs or credentials."""
    unreadable_evaluations: list[str] = []
    rows = select(project, run_id, unreadable=unreadable_evaluations)
    if run_ids is not None:
        rows = [row for row in rows if row.run_id in run_ids or row.comparison_run_id in run_ids]
    story_links: dict[str, list[JsonObject]] = {}
    unreadable_stories: list[str] = []
    identities = {row.run_id for row in rows} | {
        row.comparison_run_id for row in rows if row.comparison_run_id is not None
    }
    if run_ids is not None:
        identities.update(run_ids)
    elif run_id is not None:
        identities.add(run_id)
    else:
        identities.update(run.run_id for run in project.repository.load() if not run.pending)
    exported = []
    for identity in sorted(identities):
        run = project.repository.read(identity)
        prefix = f"runs/{identity}/"
        try:
            graph = json.loads(project.artifacts_store.read(prefix + "workflow-api.json"))
        except FileNotFoundError:
            graph = None
        exported.append(
            {
                "run": run.model_dump(mode="json", exclude_unset=True),
                "story_context": story_links.get(str(identity), []),
                "workflow_api": graph,
                "workflow_integrity": digest(graph) == run.workflow_sha256
                if graph is not None
                else None,
                "images": [
                    image_evidence(project, prefix + name, run.image_metadata.get(name))
                    for name in run.images
                ],
                "inputs": [
                    {"reference": name, "sha256": sha, "key": f"inputs/{sha}/content"}
                    for name, sha in run.input_sha256.items()
                ],
            }
        )
    referenced = {
        value for row in exported for value in row["run"].get("asset_sha256", {}).values()
    }
    referenced.update(row["run"].get("model_sha256") for row in exported)
    assets = [
        asset.model_dump(
            mode="json",
            include={
                "name",
                "role",
                "filename",
                "sha256",
                "revision",
                "license",
            },
        )
        for asset in inventory(project.models, hashes={v for v in referenced if isinstance(v, str)})
        if asset.sha256 in referenced
    ]
    return {
        "format_version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "exporter": project.identity(),
        "selection": {"run_id": str(run_id) if run_id else None},
        "unreadable_story_renders": unreadable_stories,
        "unreadable_evaluation_ids": unreadable_evaluations,
        "assets": assets,
        "evaluation_schema": Evaluation.model_json_schema(),
        "artifact_base": {
            "relative_to": "workspace",
            "key": ".artifacts/data",
            "key_type": "logical",
            "resolution": "workspace_path",
        },
        "evaluations": [row.model_dump(mode="json") for row in rows],
        "runs": exported,
    }


def image_evidence(project: RuntimeWorkspace, key: str, metadata: object) -> JsonObject:
    """Keep newly observed file checks separate from historical generation facts."""
    from komaquill.domain.models import ImageMetadata

    stored = metadata if isinstance(metadata, ImageMetadata) else None
    try:
        content = project.artifacts_store.read(key)
    except FileNotFoundError:
        observed = None
    else:
        observed = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "observed_at": datetime.now(UTC).isoformat(),
        }
        try:
            with Image.open(io.BytesIO(content)) as image:
                observed.update(width=image.width, height=image.height)
                image.verify()
            observed["valid"] = True
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
            observed["valid"] = False
    return {
        "key": key,
        "workspace_path": project.artifacts_store.path(key).relative_to(project.root).as_posix(),
        "metadata": stored.model_dump(mode="json") if stored else None,
        "observed": observed,
        "matches_record": observed["sha256"] == stored.sha256 if stored and observed else None,
    }


def write_export(output: Path, payload: JsonObject) -> None:
    """Publish complete JSON exclusively, preserving any existing destination."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        atomic_json(temporary, payload)
        try:
            os.link(temporary, output)
        except FileExistsError:
            raise KomaquillError(code="invalid_evaluation", stage="evaluation") from None
    finally:
        discard_temporary(temporary, sys.exception())
