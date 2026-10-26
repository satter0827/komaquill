"""Assemble typed story evidence from existing local records and image observations."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import JsonObject
from komaquill.domain.stories import PlannerProvenance, PlanningAttempt, PlanningOutcome, StoryInput
from komaquill.domain.story_evidence import (
    EvidenceDetail,
    EvidenceRecord,
    StoryEvidence,
)
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.evaluations import export_evidence
from komaquill.infrastructure.locations import Locations
from komaquill.infrastructure.stories.storage import FileStoryRepository

SHARED_FIELDS = frozenset(
    {
        "schema",
        "output_schema",
        "instructions",
        "source_paragraphs",
        "content",
        "correction",
        "plan",
        "planner",
        "source_text",
    }
)


class EvidenceNode(BaseModel):
    """Validate fields consumed by analysis without changing the retained graph."""

    class_type: str
    inputs: dict[str, JsonValue]


WORKFLOW = TypeAdapter(dict[str, EvidenceNode])


def workflow_parameters(graph: JsonObject | None, run_id: UUID) -> JsonObject:
    if graph is None:
        return {}
    try:
        nodes = WORKFLOW.validate_python(graph)
    except ValidationError:
        raise KomaquillError(
            code="storage_failed",
            stage="storage",
            retryable=False,
            details={"run_id": str(run_id)},
        ) from None
    return {
        identity: {
            "class_type": node.class_type,
            "inputs": {
                key: value for key, value in node.inputs.items() if not isinstance(value, list)
            },
        }
        for identity, node in nodes.items()
    }


def object_hash(value: JsonValue) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class EvidenceObjects:
    """Deduplicate original payloads while preserving raw-file hashes separately."""

    def __init__(self) -> None:
        self.objects: dict[str, JsonValue] = {}

    def add(self, value: JsonValue) -> str:
        identity = object_hash(value)
        if identity not in self.objects:
            self.objects[identity] = self.pack(value)
        return identity

    def pack(self, value: JsonValue) -> JsonValue:
        if isinstance(value, dict):
            return {
                key: {"object_ref": self.add(item)}
                if key in SHARED_FIELDS and item is not None
                else self.pack(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.pack(item) for item in value]
        return value


def _planning(raw: JsonObject, has_plan: bool) -> JsonObject:
    provenance = PlannerProvenance.model_validate(raw["provenance"]) if raw["provenance"] else None
    attempts = [PlanningAttempt.model_validate(value) for value in raw["attempts"]]
    outcome = PlanningOutcome.model_validate(raw["outcome"]) if raw["outcome"] else None
    counters = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"):
        values = [
            getattr(row.response.usage, key) if row.response and row.response.usage else None
            for row in attempts
        ]
        counters[key] = (
            sum(v for v in values if v is not None)
            if values and all(v is not None for v in values)
            else None
        )
    unknown = [key for key, value in counters.items() if value is None]
    if outcome is None and not has_plan:
        unknown.append("planning_status")
    if outcome is None:
        unknown.append("operation_elapsed_seconds")
    conditions = (
        provenance.model_dump(
            mode="json",
            exclude={
                "instructions",
                "app_identity",
                "base_url",
            },
        )
        if provenance
        else {}
    )
    return {
        "status": outcome.status if outcome else "succeeded" if has_plan else "unknown",
        "attempts": [
            {
                "attempt": row.attempt,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "failure": row.failure,
                "validation_issues": row.validation_issues,
                "model": row.response.model if row.response else None,
                "finish_reason": row.response.finish_reason if row.response else None,
                "usage": row.response.usage if row.response else None,
                "response_sha256": hashlib.sha256(row.response.content.encode()).hexdigest()
                if row.response
                else None,
            }
            for row in attempts
        ],
        "calls": len(attempts),
        "usage": counters,
        "elapsed_seconds": (outcome.finished_at - outcome.started_at).total_seconds()
        if outcome
        else None,
        "repair_limit": outcome.repair_limit
        if outcome
        else provenance.repair_attempts
        if provenance
        else None,
        "repair_limit_exhausted": outcome.status == "rejected"
        and outcome.attempts == outcome.repair_limit + 1
        if outcome
        else None,
        "failure": outcome.failure if outcome else None,
        "conditions": {
            **conditions,
            "output_schema_sha256s": [
                object_hash(row.output_schema) if row.output_schema else None for row in attempts
            ],
        },
        "producer": provenance.app_identity if provenance else None,
        "unknown_fields": unknown,
    }


def story_evidence(
    project: Project,
    repository: FileStoryRepository,
    story_id: UUID,
    snapshot: JsonObject | None,
    *,
    render_id: UUID | None,
    detail: EvidenceDetail,
) -> StoryEvidence:
    raw = repository.planning_evidence(story_id)
    request = StoryInput.model_validate(raw["request"])
    plan = repository.read(story_id) if snapshot is not None else None
    render = (
        repository.render(UUID(snapshot["render_id"]))
        if snapshot and snapshot["render_id"]
        else None
    )
    if render_id is not None and (render is None or render.render_id != render_id):
        raise KomaquillError(code="request_not_found", stage="storage", retryable=False)
    selected = [identity for scene in render.scenes for identity in scene.run_ids] if render else []
    # A reserved request may be unsent; a missing recorded history is corruption.
    placements = Locations(project).read().runs
    recorded = set()
    for identity in selected:
        if identity not in placements:
            raise KomaquillError(
                code="storage_failed",
                stage="storage",
                retryable=False,
                details={"run_id": str(identity)},
            )
        try:
            project.repository.read(identity)
        except FileNotFoundError:
            continue
        recorded.add(identity)
    common = export_evidence(project, run_ids=set(selected) & recorded)
    runs = []
    for row in common["runs"]:
        for image in row["images"]:
            image["workspace_path"] = (
                project.artifacts_store.path(image["key"]).relative_to(project.root).as_posix()
            )
        parameters = workflow_parameters(row["workflow_api"], UUID(row["run"]["run_id"]))
        runs.append(
            {
                "run": row["run"],
                "images": row["images"],
                "workflow_integrity": row["workflow_integrity"],
                "parameters": parameters,
                "inputs": row["inputs"],
            }
        )
    folder = repository.folder(story_id)
    paths = [folder / "request.json"]
    paths += [
        p
        for p in (folder / "plan.json", folder / "provenance.json", folder / "planning-result.json")
        if p.is_file()
    ]
    paths += sorted((folder / "planning").glob("[0-9]*.json"))
    paths += sorted((folder / "planning/inputs").glob("attempt-*.json"))
    if render:
        paths.append(folder / "renders" / str(render.render_id) / "render.json")
    for row in common["runs"]:
        prefix = f"runs/{row['run']['run_id']}/"
        paths += [
            project.artifacts_store.path(prefix + name)
            for name in (
                "run.json",
                "workflow-api.json",
                "workflow.json",
                "submitted-workflow-api.json",
                "submitted-workflow.json",
            )
            if project.artifacts_store.path(prefix + name).is_file()
        ]
    pool = EvidenceObjects()
    records = []
    for path in paths:
        content = path.read_bytes()
        key = path.relative_to(project.data).as_posix()
        records.append(
            EvidenceRecord(
                key=key,
                workspace_path=path.relative_to(project.root).as_posix(),
                sha256=hashlib.sha256(content).hexdigest(),
                object_ref=pool.add(json.loads(content)) if detail == "full" else None,
            )
        )
    return StoryEvidence.model_validate(
        {
            "detail": detail,
            "exported_at": datetime.now(UTC),
            "exporter": project.identity(),
            "story_id": story_id,
            "source": {
                "name": request.source_name,
                "text": request.source_text,
                "sha256": request.source_sha256,
            },
            "story": {
                key: snapshot[key]
                for key in (
                    "title",
                    "synopsis",
                    "requested_images",
                    "image_count",
                    "purpose",
                    "case_id",
                    "render_id",
                    "complete",
                    "finished",
                )
            }
            if snapshot
            else None,
            "characters": plan.characters if plan else [],
            "props": plan.props if plan and plan.format_version == 2 else None,
            "supplements": plan.supplements if plan else [],
            "scenes": snapshot["scenes"] if snapshot else [],
            "planning": _planning(raw, plan is not None),
            "render": {
                "render_id": render.render_id,
                "created_at": render.created_at,
                "plan_sha256": render.plan_sha256,
                "image_profile": render.image_profile,
                "seeds": {str(scene.scene_id): scene.seed for scene in render.scenes},
                "profiles": {
                    str(scene.scene_id): scene.image_profile or render.image_profile
                    for scene in render.scenes
                },
            }
            if render
            else None,
            "selected_run_ids": selected,
            "runs": runs,
            "evaluations": common["evaluations"],
            "unreadable_evaluation_ids": common["unreadable_evaluation_ids"],
            "assets": common["assets"],
            "records": records,
            "objects": pool.objects,
            "data_roles": {
                "source": "original",
                "narrative": "llm_retelling",
                "character_appearance": "illustration_design",
                "visual_description": "illustration_direction",
                "supplements": "llm_declared_additions",
                "source_fact_verification": "not_performed",
            },
        }
    )
