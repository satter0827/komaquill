"""Publish selections and evidence without modifying canonical story or image records."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from komaquill.api import Client, Evaluation, Evaluator
from komaquill.infrastructure.atomic import atomic_bytes, atomic_json
from komaquill.infrastructure.stories.publication import prose, write_document
from tests.live.story_completion import Case, load, project

CRITERION = "story-completion-review"
INSTRUCTION = (
    "Review subject count, species and anatomy; action and umbrella ownership; weather, "
    "grounding and continuity; appearance. Keep provisional selection separate from approval. "
    "Unknown is not a pass. Preserve all candidates and original evidence."
)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: int = Field(ge=1, le=4, strict=True)
    run_id: UUID
    image: str
    decision: Literal["accept", "reject", "undecided"]
    reason: str = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: int = Field(ge=1, le=4, strict=True)
    key: str
    reason: str = Field(min_length=1)


class ReviewFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1] = 1
    reviews: list[Review] = Field(default_factory=list)
    selections: list[Selection] = Field(default_factory=list)


def candidates(api: Client, case: Case):
    """Select only runs proven to belong to this baseline or an improvement."""
    snapshot = api.stories.show(case.story_id, render_id=case.baseline_render_id)
    result = {}
    for scene in snapshot.scenes:
        for identity in scene.attempt_ids:
            run = project(Path(api.workspace).parent).repository.read(identity)
            for name in run.images:
                metadata = run.image_metadata.get(name)
                if metadata and metadata.role == "candidate":
                    result[f"runs/{run.run_id}/{name}"] = (scene.position, run, name)
    for job in case.improvements:
        if job.run_id is None:
            continue
        run = project(Path(api.workspace).parent).repository.read(job.run_id)
        for name in run.images:
            metadata = run.image_metadata.get(name)
            if metadata and metadata.role == "candidate":
                result[f"runs/{run.run_id}/{name}"] = (job.scene, run, name)
    return snapshot, result


def publish(report: Path) -> None:
    case = load(report)
    api = Client(project(report).root)
    snapshot, images = candidates(api, case)
    path = report / "reviews.json"
    reviewed = ReviewFile.model_validate_json(path.read_bytes()) if path.exists() else ReviewFile()
    if len({row.scene for row in reviewed.selections}) != len(reviewed.selections):
        raise ValueError("Select at most one image per scene.")
    evaluation_ids = {}
    existing = api.evaluations.list()
    for row in reviewed.reviews:
        key = f"runs/{row.run_id}/{row.image}"
        if key not in images or images[key][0] != row.scene:
            raise ValueError("A review references an image outside its case scene.")
        previous = next(
            (
                item
                for item in reversed(existing)
                if item.case_id == str(case.case_id)
                and item.run_id == row.run_id
                and item.image == row.image
                and item.criterion == CRITERION
            ),
            None,
        )
        if previous and previous.decision == row.decision and previous.reason == row.reason:
            evaluation = previous
        else:
            evaluation = Evaluation(
                run_id=row.run_id,
                image=row.image,
                criterion=CRITERION,
                criterion_version="1",
                case_id=str(case.case_id),
                decision=row.decision,
                reason=row.reason,
                supersedes=previous.evaluation_id if previous else None,
                evaluator=Evaluator(
                    kind="ai", name="Codex visual review", model="unknown", instruction=INSTRUCTION
                ),
            )
            api.evaluations.add([evaluation])
            existing.append(evaluation)
        evaluation_ids[key] = str(evaluation.evaluation_id)
    selected = {}
    for row in reviewed.selections:
        if (
            row.key not in images
            or images[row.key][0] != row.scene
            or row.key not in evaluation_ids
        ):
            raise ValueError("A selection must reference a reviewed candidate from its scene.")
        selected[row.scene] = row
    all_lines = [f"# {prose(snapshot.title)}"]
    selected_lines = [f"# {prose(snapshot.title)}"]
    evidence_images = []
    for scene in snapshot.scenes:
        heading = f"## {scene.position}. {prose(scene.title)}"
        all_lines.extend([heading, prose(scene.narrative)])
        selected_lines.extend([heading, prose(scene.narrative)])
        for key, (position, run, name) in images.items():
            if position != scene.position:
                continue
            image = api.artifact_path(key)
            if not image.is_file():
                raise ValueError("A saved image reference is missing.")
            relative = image.relative_to(report).as_posix()
            metadata = run.image_metadata[name]
            all_lines.extend(
                [
                    f"![{position}]({relative})",
                    f"Run: {run.run_id}; candidate: {metadata.batch_index + 1}",
                ]
            )
            evidence_images.append(
                {
                    "scene": position,
                    "key": key,
                    "metadata": metadata.model_dump(mode="json"),
                    "evaluation_id": evaluation_ids.get(key),
                }
            )
        choice = selected.get(scene.position)
        if choice:
            image = api.artifact_path(choice.key)
            selected_lines.append(f"![{scene.position}]({image.relative_to(report).as_posix()})")
        else:
            selected_lines.append("No provisional selection has been recorded.")
    manifest = {
        "format_version": 1,
        "case_id": str(case.case_id),
        "story_id": str(case.story_id),
        "baseline_render_id": str(snapshot.render_id) if snapshot.render_id else None,
        "observed_at": datetime.now(UTC).isoformat(),
        "baseline_complete": snapshot.complete,
        "selection_complete": len(selected) == 4,
        "selection_is_quality_approval": False,
        "selections": [
            row.model_dump(mode="json")
            for row in sorted(selected.values(), key=lambda row: row.scene)
        ],
        "images": evidence_images,
        "improvements": [
            job.model_dump(mode="json", exclude={"source"}) for job in case.improvements
        ],
        "reviews": reviewed.model_dump(mode="json")["reviews"],
        "source_plan_sha256": case.source_plan_sha256,
        "new_llm_calls": 0,
        "timing": {
            "prepared_at": case.prepared_at.isoformat(),
            "gpu_started_at": case.gpu_started_at.isoformat() if case.gpu_started_at else None,
        },
    }
    atomic_json(report / "manifest.json", manifest)
    write_document(report / "all-candidates.md", all_lines)
    write_document(report / "selected.md", selected_lines)
    if (
        not case.improvements
        and len(selected) == 4
        and not (report / "baseline-manifest.json").exists()
    ):
        atomic_json(report / "baseline-manifest.json", manifest)
        atomic_bytes(report / "baseline-selection.md", (report / "selected.md").read_bytes())
    baseline_path = report / "baseline-manifest.json"
    if baseline_path.is_file():
        original = {
            row["scene"]: row for row in json.loads(baseline_path.read_bytes())["selections"]
        }
        comparison = [f"# {prose(snapshot.title)}", "## Before and after"]
        for scene in snapshot.scenes:
            comparison.append(f"## {scene.position}. {prose(scene.title)}")
            for label, choice in (
                ("Before", original.get(scene.position)),
                ("After", selected.get(scene.position)),
            ):
                if choice is None:
                    continue
                key = choice["key"] if isinstance(choice, dict) else choice.key
                image = api.artifact_path(key)
                comparison.extend([label, f"![{label}]({image.relative_to(report).as_posix()})"])
        write_document(report / "before-after.md", comparison)
    atomic_json(
        report / "story-summary.json",
        api.stories.export(case.story_id, render_id=snapshot.render_id).model_dump(mode="json"),
    )
    atomic_json(
        report / "story-full.json",
        api.stories.export(case.story_id, render_id=snapshot.render_id, detail="full").model_dump(
            mode="json"
        ),
    )
    for identity in {run.run_id for _, run, _ in images.values()}:
        atomic_json(report / "runs" / f"{identity}.json", api.evaluations.export(identity))
    write_document(
        report / "README.md",
        [
            f"# {prose(snapshot.title)}",
            f"Saved baseline scenes: {snapshot.counts.get('saved', 0)}/4. "
            f"Provisional selections: {len(selected)}/4.",
            "Storage completion and quality approval are separate.",
            "[Selected scenes](selected.md) · [All candidates](all-candidates.md) · "
            "[Manifest](manifest.json)",
        ],
    )
