"""Freeze reviewed real plans for the bounded Klein and WAI verification."""

import json
import tomllib
from pathlib import Path
from uuid import UUID

from komaquill.api import Client
from komaquill.application.stories.image_inputs import scene_input
from komaquill.domain.images import preset_fingerprint
from komaquill.domain.workflow import digest
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.stories.storage import FileStoryRepository
from tests.live.klein_comparison import ComparisonConfig, Job, runner_fingerprint, scene_conditions
from tests.live.story_suite import PlanReview, load
from tests.support.story_cases import cases


def reviewed_plans(suite_report: Path) -> dict:
    """Require source-matched, attributed approval of actual saved LLM plans."""
    suite = load(suite_report)
    corpus = cases()
    reviews = [
        PlanReview.model_validate(row)
        for row in json.loads((suite_report / "planning-reviews.json").read_bytes())
    ]
    repository = FileStoryRepository(Project(suite_report / "planning"))
    selected = {}
    for case_id in suite.config.live_cases:
        for item in reversed(suite.plans.get(case_id, [])):
            if item["status"] != "saved" or not item.get("story_id"):
                continue
            story_id = UUID(item["story_id"])
            plan = repository.read(story_id)
            envelope = json.loads((repository.folder(story_id) / "plan.json").read_bytes())
            matching = [row for row in reviews if row.story_id == story_id]
            if not matching:
                continue
            review = matching[-1]
            if (
                review.decision == "accept"
                and review.case_id == case_id
                and review.source_sha256
                == plan.request.source_sha256
                == corpus[case_id].source_sha256
                and review.plan_sha256 == envelope["sha256"]
                and review.evaluator.kind == "ai"
                and plan.planner.provider != "fake"
            ):
                selected[case_id] = plan
                break
    if set(selected) != set(suite.config.live_cases):
        raise ValueError("Every final story requires a semantically approved real LLM plan.")
    return selected


def freeze(report: Path, suite_report: Path) -> None:
    """Resolve all baseline jobs from the same approved plans before GPU allocation."""
    if (report / "comparison.json").exists():
        raise ValueError("Resume the immutable verification instead of overwriting it.")
    specification = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/story-validation/config.toml").read_text(
            encoding="utf-8"
        )
    )
    config = ComparisonConfig.model_validate(specification["comparison"])
    plans = reviewed_plans(suite_report)
    if set(plans) != set(config.cases):
        raise ValueError("The reviewed plans differ from the frozen verification cases.")
    project = Project(report / "workspace")
    Client.initialize(project.root)
    (project.root / "komaquill.toml").write_text(
        'models = ["wai-illustrious", "klein-4b"]\n', encoding="utf-8"
    )
    settings = project.settings()
    corpus = cases()
    jobs = []
    wai_cases = set(specification["wai_cases"])
    for model in config.models:
        profile = settings.presets[model]
        for case_id, plan in plans.items():
            if model == "wai-illustrious" and case_id not in wai_cases:
                continue
            seeds = (
                config.seeds
                if model == "klein-4b" and corpus[case_id].group == "confirmation"
                else config.seeds[:1]
            )
            for seed in seeds:
                for scene in plan.scenes:
                    compiled = scene_input(plan, scene, profile)
                    jobs.append(
                        Job(
                            id=f"baseline-{case_id}-{scene.position}-{model}-{seed}",
                            group="baseline",
                            case=case_id,
                            scene=scene.position,
                            profile=model,
                            seed=seed + scene.position - 1,
                            prompt=compiled.prompt,
                            subjects=compiled.subject_prompts,
                            preset_sha256=preset_fingerprint(profile),
                        )
                    )
    if len(jobs) != config.baseline_limit:
        raise ValueError("Baseline jobs differ from the approved verification allocation.")
    frozen = {
        "format_version": 2,
        "config": config.model_dump(mode="json"),
        "identity": project.identity(),
        "runner_sha256": runner_fingerprint(),
        "plans": {key: plan.model_dump(mode="json") for key, plan in plans.items()},
        "conditions": {
            key: {
                "group": corpus[key].group,
                "source_sha256": corpus[key].source_sha256,
                "scenes": {
                    str(scene.position): scene_conditions(key, scene) for scene in plan.scenes
                },
                "source": {
                    f"{key}:source:{index}": text
                    for index, text in enumerate(corpus[key].required, 1)
                },
                "presentation": {},
            }
            for key, plan in plans.items()
        },
        "jobs": [job.model_dump(mode="json") for job in jobs],
    }
    atomic_json(report / "comparison.json", frozen)
    atomic_json(
        report / "state.json",
        {"format_version": 2, "sessions": {}, "jobs": {}, "comparison_sha256": digest(frozen)},
    )
