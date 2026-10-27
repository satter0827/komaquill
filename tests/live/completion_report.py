"""Publish selections and evidence without modifying canonical story or image records."""

import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from filelock import FileLock
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from komaquill.api import Client, Evaluation, Evaluator
from komaquill.infrastructure.artifacts import file_digest
from komaquill.infrastructure.atomic import atomic_bytes, atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.stories.publication import prose, write_document
from tests.live.story_completion import Case, load, project, save

CRITERION = "story-completion-review"
REQUIRED_CHECKS = (
    "source_action",
    "subject_count",
    "prop_color_count",
    "ownership_state",
    "anatomy",
    "continuity",
    "appearance",
)
INSTRUCTION = (
    "Review subject count, species and anatomy; source actions and prop ownership; weather, "
    "grounding and continuity; appearance. Keep provisional selection separate from approval. "
    "Unknown is not a pass. Preserve all candidates and original evidence."
    " Record each required criterion once: " + ", ".join(REQUIRED_CHECKS) + "."
)


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion: str = Field(min_length=1)
    verdict: Literal["match", "mismatch", "unclear"]
    reason: str = Field(min_length=1)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: int = Field(ge=1, le=8, strict=True)
    run_id: UUID
    image: str
    scope: Literal["local", "scene", "work"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    decision: Literal["accept", "reject", "undecided"]
    reason: str = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)
    checks: list[Check] = Field(min_length=len(REQUIRED_CHECKS))

    @model_validator(mode="after")
    def consistent_decision(self):
        criteria = [row.criterion for row in self.checks]
        if len(criteria) != len(set(criteria)) or not set(REQUIRED_CHECKS).issubset(criteria):
            raise ValueError("A review must cover every required criterion exactly once.")
        verdicts = {row.verdict for row in self.checks}
        if self.decision == "accept" and verdicts != {"match"}:
            raise ValueError("Approval requires every recorded check to match.")
        return self


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: int = Field(ge=1, le=8, strict=True)
    key: str
    reason: str = Field(min_length=1)
    source: Literal["llm", "rule"] = "llm"


class ReviewFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[2] = 2
    evaluator: Evaluator | None = None
    status: Literal["complete", "undecided", "timeout", "failed"] = "complete"
    reviews: list[Review] = Field(default_factory=list)
    selections: list[Selection] = Field(default_factory=list)


def candidates(api: Client, case: Case):
    """Select only runs proven to belong to this baseline or an improvement."""
    snapshot = api.stories.show(case.story_id, render_id=case.baseline_render_id)
    result = {}
    for scene in snapshot.scenes:
        for identity in scene.attempt_ids:
            run = Project(api.workspace).repository.read(identity)
            for name in run.images:
                metadata = run.image_metadata.get(name)
                if metadata and metadata.role == "candidate":
                    result[f"runs/{run.run_id}/{name}"] = (scene.position, run, name)
    for job in case.improvements:
        if job.run_id is None:
            continue
        run = Project(api.workspace).repository.read(job.run_id)
        for name in run.images:
            metadata = run.image_metadata.get(name)
            if metadata and metadata.role == "candidate":
                result[f"runs/{run.run_id}/{name}"] = (job.scene, run, name)
    return snapshot, result


def publish(report: Path) -> None:
    with FileLock(str(report / ".case.lock"), timeout=0):
        publish_locked(report)


def publish_locked(report: Path) -> None:
    case = load(report)
    api = Client(project(report).root)
    snapshot, images = candidates(api, case)
    path = report / "reviews.json"
    if path.is_file():
        import hashlib

        raw = path.read_bytes()
        retained = report / "reviews" / (hashlib.sha256(raw).hexdigest() + ".json")
        if not retained.exists():
            atomic_bytes(retained, raw)
    review_status = "absent"
    try:
        reviewed = (
            ReviewFile.model_validate_json(path.read_bytes()) if path.exists() else ReviewFile()
        )
        if path.exists():
            review_status = "available" if reviewed.status == "complete" else reviewed.status
    except (ValueError, OSError):
        reviewed = ReviewFile()
        review_status = "invalid"
    unavailable = []
    valid_images = {}
    for key, (position, run, name) in images.items():
        try:
            image = api.artifact_path(key)
            metadata = run.image_metadata[name]
            if file_digest(image) != metadata.sha256 or image.stat().st_size != metadata.size_bytes:
                raise ValueError
            with Image.open(image) as opened:
                if opened.size != (metadata.width, metadata.height):
                    raise ValueError
                opened.verify()
        except (OSError, ValueError):
            unavailable.append(key)
        else:
            valid_images[key] = (position, run, name)
    images = valid_images
    if reviewed.reviews and reviewed.evaluator is None:
        reviewed = reviewed.model_copy(update={"reviews": []})
        review_status = "missing_evaluator"
    evaluation_ids = {}
    decisions = {}
    existing = api.evaluations.list()
    for row in reviewed.reviews:
        if reviewed.evaluator is None:
            continue
        key = f"runs/{row.run_id}/{row.image}"
        if key not in images or images[key][0] != row.scene:
            continue
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
                evaluator=reviewed.evaluator,
            )
            api.evaluations.add([evaluation])
            existing.append(evaluation)
        evaluation_ids[key] = str(evaluation.evaluation_id)
        decisions[key] = evaluation.decision
    selected = {}
    requested = {}
    for row in reviewed.selections if reviewed.status == "complete" else []:
        if row.key in images and images[row.key][0] == row.scene:
            requested.setdefault(row.scene, []).append(row)
    previous = case.selections
    for scene in snapshot.scenes:
        choices = requested.get(scene.position, [])
        if len(choices) == 1:
            selected[scene.position] = choices[0].model_copy(update={"source": "llm"})
            continue
        old = previous.get(scene.position)
        eligible = [key for key, (position, _, _) in images.items() if position == scene.position]
        key = old if old in eligible else next(iter(eligible), None)
        if key is not None:
            selected[scene.position] = Selection(
                scene=scene.position,
                key=key,
                source="rule",
                reason="Retained the previous selection."
                if key == old
                else "Selected the first valid saved candidate in generation and batch order.",
            )
    case.selections = {scene: row.key for scene, row in selected.items()}
    save(report, case)
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
            relative = Path(os.path.relpath(image, report)).as_posix()
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
            relative = Path(os.path.relpath(image, report)).as_posix()
            selected_lines.append(f"![{scene.position}]({relative})")
        else:
            selected_lines.append("No provisional selection has been recorded.")
    quality_counts = Counter(decisions.get(row.key, "unreviewed") for row in selected.values())
    quality_counts["unreviewed"] += case.scene_count - len(selected)
    manifest = {
        "format_version": 2,
        "case_id": str(case.case_id),
        "story_id": str(case.story_id),
        "baseline_render_id": str(snapshot.render_id) if snapshot.render_id else None,
        "observed_at": datetime.now(UTC).isoformat(),
        "baseline_complete": snapshot.complete,
        "selection_complete": len(selected) == case.scene_count,
        "selection_is_quality_approval": False,
        "quality_passed": review_status == "available"
        and quality_counts["accept"] == case.scene_count,
        "selected_quality": {
            decision: quality_counts[decision]
            for decision in ("accept", "reject", "undecided", "unreviewed")
        },
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
        "planning_llm_calls": len(api.stories.export(case.story_id).planning.attempts),
        "review_llm_calls": None,
        "review_status": review_status,
        "unavailable_images": unavailable,
        "timing": {
            "prepared_at": case.prepared_at.isoformat(),
            "gpu_started_at": case.gpu_started_at.isoformat() if case.gpu_started_at else None,
        },
    }
    manifest["page"] = {"complete": False}
    if len(selected) == case.scene_count:
        from tests.live.completion_page import render_page

        try:
            manifest["page"] = render_page(
                report,
                snapshot.title,
                [
                    {
                        "scene": scene.position,
                        "key": selected[scene.position].key,
                        "path": api.artifact_path(selected[scene.position].key),
                        "text": scene.narrative,
                    }
                    for scene in snapshot.scenes
                ],
                expected_scenes=case.scene_count,
            )
        except (ValueError, OSError) as error:
            manifest["page"] = {"complete": False, "failure_type": type(error).__name__}
    manifest["comic_complete"] = bool(manifest["page"]["complete"])
    atomic_json(report / "manifest.json", manifest)
    write_document(report / "all-candidates.md", all_lines)
    write_document(report / "selected.md", selected_lines)
    baseline_keys = {candidate.key for scene in snapshot.scenes for candidate in scene.candidates}
    if (
        len(selected) == case.scene_count
        and all(row.key in baseline_keys for row in selected.values())
        and not (report / "baseline-manifest.json").exists()
    ):
        baseline = {
            **manifest,
            "images": [row for row in evidence_images if row["key"] in baseline_keys],
            "improvements": [],
            "reviews": [
                row
                for row in manifest["reviews"]
                if f"runs/{row['run_id']}/{row['image']}" in baseline_keys
            ],
        }
        atomic_json(report / "baseline-manifest.json", baseline)
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
                relative = Path(os.path.relpath(image, report)).as_posix()
                comparison.extend([label, f"![{label}]({relative})"])
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
            f"Saved baseline scenes: {snapshot.counts.get('saved', 0)}/{case.scene_count}. "
            f"Provisional selections: {len(selected)}/{case.scene_count}.",
            "Storage completion and quality approval are separate.",
            "[Selected scenes](selected.md) · [All candidates](all-candidates.md) · "
            "[Manifest](manifest.json)",
        ],
    )
