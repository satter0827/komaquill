"""Persist evaluations and export bounded evidence for external AI analysis."""

import hashlib
import json
import os
import sys
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from filelock import FileLock

from komaquill.domain.errors import KomaquillError
from komaquill.domain.evaluations import Evaluation
from komaquill.domain.models import JsonObject
from komaquill.domain.workflow import digest
from komaquill.infrastructure.asset_store import inventory
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.storage import atomic_json, discard_temporary


class FileEvaluationRepository:
    """Append records under a single lock without changing original evaluations."""

    def __init__(self, project: Project) -> None:
        self.project = project

    def lock(self) -> AbstractContextManager[object]:
        self.project.state.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.project.state / ".evaluations.lock"))

    def load(self) -> list[Evaluation]:
        return sorted(
            (
                Evaluation.model_validate_json(path.read_bytes())
                for path in self.project.evaluations.glob("*.json")
            ),
            key=lambda row: (row.created_at, str(row.evaluation_id)),
        )

    def append(self, evaluation: Evaluation) -> None:
        path = self.project.evaluations / f"{evaluation.evaluation_id}.json"
        if path.is_file():
            if Evaluation.model_validate_json(path.read_bytes()) != evaluation:
                raise KomaquillError(code="invalid_evaluation", stage="evaluation")
            return
        atomic_json(path, evaluation.model_dump(mode="json"))


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


def select(project: Project, run_id: UUID | None = None) -> list[Evaluation]:
    """Filter records by either side of a comparison."""
    rows = FileEvaluationRepository(project).load()
    return [row for row in rows if run_id is None or run_id in {row.run_id, row.comparison_run_id}]


def export_evidence(project: Project, run_id: UUID | None = None) -> JsonObject:
    """Expose saved conditions and artifact keys without logs or credentials."""
    rows = select(project, run_id)
    story_links, unreadable_stories = story_associations(project)
    identities = {row.run_id for row in rows} | {
        row.comparison_run_id for row in rows if row.comparison_run_id is not None
    }
    if run_id is not None:
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
        for asset in inventory(project.models)
        if asset.sha256 in referenced
    ]
    return {
        "format_version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "exporter": project.identity(),
        "selection": {"run_id": str(run_id) if run_id else None},
        "unreadable_story_renders": unreadable_stories,
        "assets": assets,
        "evaluation_schema": Evaluation.model_json_schema(),
        "artifact_base": {"relative_to": "workspace", "key": ".artifacts/data"},
        "evaluations": [row.model_dump(mode="json") for row in rows],
        "runs": exported,
    }


def image_evidence(project: Project, key: str, metadata: object) -> JsonObject:
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
    return {
        "key": key,
        "metadata": stored.model_dump(mode="json") if stored else None,
        "observed": observed,
        "matches_record": observed["sha256"] == stored.sha256 if stored and observed else None,
    }


def story_associations(project: Project) -> tuple[dict[str, list[JsonObject]], list[str]]:
    """Keep corrupt optional story records from hiding valid image evidence."""
    from komaquill.infrastructure.stories.storage import FileStoryRepository

    repository = FileStoryRepository(project)
    result: dict[str, list[JsonObject]] = {}
    unreadable = []
    for path in repository.root.glob("*/renders/*/render.json"):
        try:
            render = repository.render(UUID(path.parent.name))
            plan = repository.read(render.story_id)
            if render.plan_sha256 != digest(plan.model_dump(mode="json")):
                raise ValueError("The plan changed after rendering.")
            scenes = {scene.scene_id: scene for scene in plan.scenes}
            pending: dict[str, JsonObject] = {}
            for link in render.scenes:
                scene = scenes[link.scene_id]
                context = {
                    "story_id": str(render.story_id),
                    "render_id": str(render.render_id),
                    "scene_id": str(scene.scene_id),
                    "position": scene.position,
                    "title": scene.title,
                    "narrative": scene.narrative,
                    "source_sha256": plan.request.source_sha256,
                    "source_refs": scene.source_refs,
                    "plan_sha256": render.plan_sha256,
                    "purpose": plan.request.purpose,
                    "case_id": plan.request.case_id,
                    "planner_provider": plan.planner.provider,
                    "planner_model": plan.planner.configured_model,
                }
                pending.update({str(identity): context for identity in link.run_ids})
        except (OSError, ValueError, KeyError, KomaquillError):
            unreadable.append(path.parent.name)
            continue
        for identity, context in pending.items():
            result.setdefault(identity, []).append(context)
    return result, unreadable


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
