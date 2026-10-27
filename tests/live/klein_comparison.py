"""Compare frozen Klein configurations without calling a planning model."""

import argparse
import hashlib
import json
import os
import shutil
import time
import tomllib
from collections import Counter
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, model_validator

from komaquill.api import Client, Evaluation, ImageInput, InputArtifact, PromptSource
from komaquill.application.generation import generation_settings
from komaquill.application.stories.image_inputs import scene_input
from komaquill.domain.errors import KomaquillError, failure_info
from komaquill.domain.images import compile_image_input, preset_fingerprint
from komaquill.domain.stories import PlannerProvenance, Scene, StoryPlan, validate_draft
from komaquill.domain.workflow import digest
from komaquill.infrastructure.asset_store import cached_path, file_digest, model_path, register
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.colab import Colab
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.monitor_state import read_monitor
from komaquill.infrastructure.sessions.connection import connected
from komaquill.infrastructure.workflows import assets
from komaquill.infrastructure.workflows import prepare as prepare_workflow
from tests.live.completion_page import render_page
from tests.live.completion_report import Review, ReviewFile
from tests.support.stories import request
from tests.support.story_cases import cases

CONFIG = Path(__file__).resolve().parents[1] / "fixtures/klein-comparison/config.toml"
IMAGE_CRITERION = "klein-comparison-image"
PAIR_CRITERION = "klein-comparison-pair"


def job_workflow(project: Project, job: "Job", run_id: UUID | None = None):
    settings = generation_settings(project.settings(), job.profile, 1, job.parameters)
    profile = settings.presets[job.profile]
    if preset_fingerprint(profile) != job.preset_sha256:
        raise ValueError("The frozen profile changed.")
    compiled = compile_image_input(profile, job.prompt, job.subjects, job.inputs)
    return prepare_workflow(
        job.prompt,
        job.seed,
        str(run_id or uuid5(NAMESPACE_URL, job.id)),
        settings=settings,
        profile_name=job.profile,
        compiled_input=compiled,
    )


def ui_matches(project: Project, api: Client, job: "Job", run, *, api_source: bool = False) -> bool:
    expected, _ = job_workflow(project, job, run.run_id if api_source else None)
    actual = json.loads(api.artifact_path(f"runs/{run.run_id}/workflow-api.json").read_bytes())
    if digest(actual) != run.workflow_sha256 or set(expected) != set(actual):
        return False
    for binding in project.settings().presets[job.profile].bindings:
        if not binding.source.startswith("input_"):
            continue
        slot = binding.source.removeprefix("input_")
        reference = actual[binding.api_node]["inputs"][binding.api_input]
        if run.input_sha256.get(reference) != job.inputs[slot].sha256:
            return False
        actual[binding.api_node]["inputs"][binding.api_input] = expected[binding.api_node][
            "inputs"
        ][binding.api_input]
    return all(
        actual[key]["class_type"] == node["class_type"] and actual[key]["inputs"] == node["inputs"]
        for key, node in expected.items()
    )


def export_ui(report: Path, job_id: str, view: Literal["app", "graph"]) -> Path:
    _, config, jobs = load(report)
    job = next(row for row in jobs if row.id == job_id)
    project = Project(report / "workspace")
    path = report / "interfaces" / (job.id + "-" + view + ".json")
    with FileLock(str(report / ".operation.lock"), timeout=0):
        state = json.loads((report / "state.json").read_bytes())
        if job.id in state["jobs"]:
            if state["jobs"][job.id].get("interface") != view or not path.exists():
                raise ValueError("Reconcile the reserved interface request before proceeding.")
            return path
        if len(state["jobs"]) >= config.output_limit:
            raise ValueError("The image budget is exhausted.")
        state["jobs"][job.id] = {"status": "reserved", "interface": view}
        atomic_json(report / "state.json", state)
    _, workflow = job_workflow(project, job)
    if job.inputs:
        with connected(project) as (backend, _):
            for item in job.inputs.values():
                backend.upload_image(item, project.artifacts_store.input_content(item))
    workflow["extra"]["linearMode"] = view == "app"
    atomic_json(path, workflow)
    return path


def adopt_ui(report: Path, job_id: str) -> None:
    _, _, jobs = load(report)
    job = next(row for row in jobs if row.id == job_id)
    api = Client(report / "workspace")
    project = Project(api.workspace)
    matches = []
    for run in project.repository.load():
        try:
            if ui_matches(project, api, job, run):
                matches.append(run)
        except (OSError, ValueError, KomaquillError, KeyError, TypeError):
            continue
    if len(matches) != 1:
        raise ValueError("The interface request is missing or ambiguous; do not submit it again.")
    run = matches[0]
    with FileLock(str(report / ".operation.lock"), timeout=0):
        state = json.loads((report / "state.json").read_bytes())
        row = state["jobs"][job.id]
        row.update(status=run.state, run_id=str(run.run_id), workflow_sha256=run.workflow_sha256)
        atomic_json(report / "state.json", state)


def saved_candidate(project: Project, api: Client, job: "Job", record: dict):
    run = project.repository.read(UUID(record["run_id"]))
    api_source = job.source in run.input_sources
    if (
        run.state != "saved"
        or (not api_source and record.get("interface") not in {"app", "graph"})
        or not ui_matches(project, api, job, run, api_source=api_source)
    ):
        raise ValueError("The candidate does not belong to the frozen job.")
    for name in run.images:
        metadata = run.image_metadata.get(name)
        if metadata is None or metadata.role != "candidate":
            continue
        key = f"runs/{run.run_id}/{name}"
        path = api.artifact_path(key)
        if path.is_file() and file_digest(path) == metadata.sha256:
            return run, name, key, path
    raise ValueError("No valid saved candidate is available.")


def record_reviews(report: Path, path: Path) -> None:
    frozen, _, jobs = load(report)
    value = ReviewFile.model_validate_json(path.read_bytes())
    if value.status != "complete" or value.evaluator is None or value.evaluator.kind != "ai":
        raise ValueError("An attributed AI image review is required.")
    api = Client(report / "workspace")
    project = Project(api.workspace)
    state = json.loads((report / "state.json").read_bytes())
    for row in value.reviews:
        matching = [
            job
            for job in jobs
            if state["jobs"].get(job.id, {}).get("run_id") == str(row.run_id)
            and job.scene == row.scene
        ]
        if len(matching) != 1:
            raise ValueError("The reviewed image does not identify one frozen job.")
        job = matching[0]
        run, name, _, _ = saved_candidate(project, api, job, state["jobs"][job.id])
        if row.image != name:
            raise ValueError("The review identifies a different candidate.")
        reason = json.dumps(
            {
                "review": row.model_dump(mode="json"),
                "conditions_sha256": digest(frozen["conditions"][job.case]),
                "image_sha256": run.image_metadata[name].sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        prior = [
            item
            for item in api.evaluations.list(run.run_id)
            if item.criterion == IMAGE_CRITERION and item.case_id == job.id
        ]
        previous = max(prior, key=lambda item: item.created_at) if prior else None
        if previous and previous.reason == reason and previous.evaluator == value.evaluator:
            continue
        api.evaluations.add(
            [
                Evaluation(
                    run_id=run.run_id,
                    image=name,
                    criterion=IMAGE_CRITERION,
                    decision=row.decision,
                    reason=reason,
                    evaluator=value.evaluator,
                    case_id=job.id,
                    supersedes=previous.evaluation_id if previous else None,
                )
            ]
        )


class ComparisonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cases: tuple[str, ...]
    models: tuple[str, ...]
    seeds: tuple[int, ...]
    baseline_limit: int = Field(gt=0)
    edit_limit: int = Field(ge=0)
    ablation_limit: int = Field(ge=0)
    regression_limit: int = Field(ge=0)
    technical_retry_limit: int = Field(ge=0)
    output_limit: int = Field(gt=0)
    compute_unit_limit: float = Field(gt=0)
    compute_unit_reserve: float = Field(ge=0)
    submission_seconds: int = Field(gt=0)
    session_seconds: int = Field(gt=0)
    storage_bytes: int = Field(gt=0)
    minimum_free_bytes: int = Field(gt=0)
    presentation: dict[str, str]

    @model_validator(mode="after")
    def bounded(self):
        if (
            self.output_limit
            != sum(
                (
                    self.baseline_limit,
                    self.edit_limit,
                    self.ablation_limit,
                    self.regression_limit,
                    self.technical_retry_limit,
                )
            )
            or self.submission_seconds >= self.session_seconds
        ):
            raise ValueError("The comparison budgets are inconsistent.")
        return self


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    group: Literal["baseline", "edit", "ablation", "regression"]
    case: str
    scene: int
    profile: str
    seed: int
    prompt: str
    subjects: tuple[str, ...]
    inputs: dict[str, InputArtifact] = Field(default_factory=dict)
    parameters: dict[str, float] = Field(default_factory=dict)
    preset_sha256: str
    parent_sha256: str | None = None
    variant: str | None = None

    @property
    def source(self) -> PromptSource:
        content = ImageInput(
            prompt=self.prompt, subjects=self.subjects, parameters=self.parameters
        ).model_dump_json()
        return PromptSource(
            name=self.id + ".json",
            role="input",
            content=content,
            sha256=hashlib.sha256(content.encode()).hexdigest(),
        )


def configuration() -> ComparisonConfig:
    return ComparisonConfig.model_validate(tomllib.loads(CONFIG.read_text(encoding="utf-8")))


def fixed_plan(case_id: str, config: ComparisonConfig) -> StoryPlan:
    """Keep source obligations separate from added, shared presentation choices."""
    case = cases()[case_id]
    data = case.draft.model_dump(mode="json")
    for character in data["characters"]:
        character["appearance"] = config.presentation.get(character["key"], character["appearance"])
    for prop in data["props"]:
        prop["appearance"] = config.presentation.get(prop["key"], prop["appearance"])
    # Preserve the source action and ending rather than the earlier simplified pose.
    if case_id == "rainy-cat":
        data["scenes"][2]["subjects"][0]["action"] = "runs toward its mother beneath the awning"
        data["scenes"][3]["subjects"].extend(
            [
                {"character": "mother_cat", "action": "nuzzles the kitten beneath the awning"},
                {"character": "kitten", "action": "rests beside its mother beneath the awning"},
            ]
        )
    from komaquill.domain.stories import StoryDraft

    original = request(4, case.source)
    draft = StoryDraft.model_validate(data)
    validate_draft(draft, original)
    instruction = "Use source-checked fixed specifications; no planning-model result is measured."
    return StoryPlan(
        format_version=2,
        request=original,
        title=draft.title,
        synopsis=draft.synopsis,
        characters=draft.characters,
        props=draft.props,
        supplements=draft.supplements,
        scenes=[
            Scene(
                **scene.model_dump(),
                characters=[subject.character for subject in scene.subjects],
                scene_id=uuid5(NAMESPACE_URL, f"{case_id}:{index}"),
                position=index,
            )
            for index, scene in enumerate(draft.scenes, 1)
        ],
        planner=PlannerProvenance(
            profile="source-checked-specification",
            provider="curated",
            configured_model="none",
            instructions=instruction,
            instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
            timeout_seconds=1,
            max_output_tokens=1,
            planning_policy={"style": "manga illustration"},
        ),
    )


def prepare(report: Path, parents_workspace: Path, parents_file: Path) -> None:
    """Freeze every job, parent and expectation before any GPU is requested."""
    if report.exists():
        raise ValueError("Use the existing frozen comparison instead of overwriting it.")
    config = configuration()
    client = Client.initialize(report / "workspace")
    project = Project(client.workspace)
    settings = project.settings()
    plans = {key: fixed_plan(key, config) for key in config.cases}
    jobs = []
    for model in config.models:
        profile = settings.presets[model]
        for case_id, plan in plans.items():
            for seed in config.seeds:
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
        for scene in plans["red-book"].scenes[:2]:
            compiled = scene_input(plans["red-book"], scene, profile)
            parameters = {"party_strength": 0.0, "fixes_strength": 0.0}
            jobs.append(
                Job(
                    id=f"ablation-red-book-{scene.position}-{model}",
                    group="ablation",
                    case="red-book",
                    scene=scene.position,
                    profile=model,
                    seed=config.seeds[0] + scene.position - 1,
                    prompt=compiled.prompt,
                    subjects=compiled.subject_prompts,
                    parameters=parameters,
                    preset_sha256=preset_fingerprint(profile.configured_parameters(parameters)),
                )
            )
    parents = json.loads(parents_file.read_bytes())
    origin = Client(parents_workspace)
    for index, parent in enumerate(parents, 1):
        path = origin.artifact_path(parent["key"])
        artifact = client.inputs.import_file(path)
        if artifact.sha256 != parent["sha256"]:
            raise ValueError("A common edit parent changed.")
        for model in config.models:
            profile_name = model + "-edit"
            for variant in ("original", "visible_state"):
                jobs.append(
                    Job(
                        id=f"edit-{index}-{variant}-{model}",
                        group="edit",
                        case="red-book",
                        scene=parent["scene"],
                        profile=profile_name,
                        seed=config.seeds[0] + parent["scene"] - 1,
                        prompt=parent[variant],
                        subjects=(),
                        inputs={"reference": artifact},
                        parent_sha256=artifact.sha256,
                        variant=variant,
                        preset_sha256=preset_fingerprint(settings.presets[profile_name]),
                    )
                )
    regression = next(
        job
        for job in jobs
        if job.group == "baseline"
        and job.case == "red-book"
        and job.scene == 1
        and job.profile == "klein-4b"
    )
    jobs.append(regression.model_copy(update={"id": "regression-generate", "group": "regression"}))
    regression = next(job for job in jobs if job.group == "edit" and job.profile == "klein-4b-edit")
    jobs.append(regression.model_copy(update={"id": "regression-edit", "group": "regression"}))
    counts = Counter(job.group for job in jobs)
    if counts != {
        "baseline": config.baseline_limit,
        "edit": config.edit_limit,
        "ablation": config.ablation_limit,
        "regression": config.regression_limit,
    }:
        raise ValueError("The frozen jobs do not match the approved image budgets.")
    atomic_json(
        report / "comparison.json",
        {
            "format_version": 1,
            "config": config.model_dump(mode="json"),
            "identity": project.identity(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "plans": {key: plan.model_dump(mode="json") for key, plan in plans.items()},
            "conditions": {
                key: {
                    "source_sha256": cases()[key].source_sha256,
                    "source": {
                        f"{key}:source:{index}": value
                        for index, value in enumerate(cases()[key].required, 1)
                    },
                    "presentation": config.presentation,
                }
                for key in config.cases
            },
            "jobs": [job.model_dump(mode="json") for job in jobs],
        },
    )
    atomic_json(
        report / "state.json",
        {
            "format_version": 1,
            "sessions": {},
            "jobs": {},
            "comparison_sha256": digest(json.loads((report / "comparison.json").read_bytes())),
        },
    )


def load(report: Path):
    frozen = json.loads((report / "comparison.json").read_bytes())
    state = json.loads((report / "state.json").read_bytes())
    if (
        frozen.get("format_version") != 1
        or state.get("format_version") != 1
        or digest(frozen) != state.get("comparison_sha256")
    ):
        raise ValueError("The frozen comparison or its version changed.")
    if frozen["runner_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError("The comparison runner changed after preparation.")
    config = ComparisonConfig.model_validate(frozen["config"])
    jobs = [Job.model_validate(row) for row in frozen["jobs"]]
    if frozen["identity"] != Project(report / "workspace").identity():
        raise ValueError("The executable code changed after the comparison was frozen.")
    return frozen, config, jobs


def start(report: Path, models_workspace: Path, gpu: Literal["T4", "A100"]) -> None:
    """Prepare only selected assets and preserve the shared CU baseline."""
    _, config, _ = load(report)
    project = Project(report / "workspace")
    with FileLock(str(report / ".operation.lock"), timeout=0):
        state = json.loads((report / "state.json").read_bytes())
        if gpu in state["sessions"]:
            receipt = Colab(project).receipt()
            if receipt is None or receipt.settings is None or receipt.settings.gpu != gpu:
                raise ValueError("This GPU session has ended or its ownership is unknown.")
        profiles = [
            name
            for model in (["klein-4b"] if gpu == "T4" else config.models)
            for name in (model, model + "-edit")
        ]
        values = {
            "profiles": profiles,
            "gpu": gpu,
            "high_ram": gpu == "A100",
            "compute_unit_limit": config.compute_unit_limit,
            "compute_unit_reserve": config.compute_unit_reserve,
            "submission_timeout": config.submission_seconds,
            "session_timeout": config.session_seconds,
        }
        (project.root / "komaquill.toml").write_text(
            "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items()) + "\n",
            encoding="utf-8",
        )
        if shutil.disk_usage(project.root).free < config.minimum_free_bytes:
            raise ValueError("The required disk reserve is unavailable.")
        for model in assets(project.settings()):
            source = cached_path(Project(models_workspace).models, model)
            if source is None:
                raise ValueError("A selected model has not been verified locally.")
            target = model_path(project.models, model)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                os.link(source, target)
            if cached_path(project.models, model) is None:
                from komaquill.infrastructure.asset_store import verify_file

                if not verify_file(project.models, model):
                    raise ValueError("A shared model failed verification.")
            register(project.models, model, target)
        state["sessions"].setdefault(gpu, {"started_at": datetime.now(UTC).isoformat()})
        state["sessions"][gpu]["status"] = "requested"
        atomic_json(report / "state.json", state)
    try:
        Client(project.root).sessions.start(profiles=profiles, gpu=gpu, high_ram=gpu == "A100")
    except BaseException as error:
        with FileLock(str(report / ".operation.lock"), timeout=0):
            state = json.loads((report / "state.json").read_bytes())
            state["sessions"][gpu].setdefault(
                "first_failure", failure_info(error, "preparation").model_dump(mode="json")
            )
            atomic_json(report / "state.json", state)
        try:
            stop(report)
        except BaseException as cleanup:
            error.add_note(f"Shutdown did not complete: {type(cleanup).__name__}")
        raise
    with FileLock(str(report / ".operation.lock"), timeout=0):
        state = json.loads((report / "state.json").read_bytes())
        receipt = Colab(project).receipt()
        state["sessions"][gpu].update(
            status="ready", runtime_id=str(receipt.runtime_id) if receipt else None
        )
        atomic_json(report / "state.json", state)


def stop(report: Path) -> None:
    """Keep shutdown available even if executable files changed after preparation."""
    project = Project(report / "workspace")
    receipt = Colab(project).receipt()
    monitor = read_monitor(project)
    Client(project.root).sessions.stop()
    runtime_id = receipt.runtime_id if receipt else monitor.runtime_id if monitor else None
    settings = receipt.settings if receipt else monitor.settings if monitor else None
    if runtime_id is None or settings is None:
        return
    outcome_path = project.logs / str(runtime_id) / "shutdown.json"
    outcome = json.loads(outcome_path.read_bytes()) if outcome_path.exists() else {}
    with FileLock(str(report / ".operation.lock"), timeout=0):
        state = json.loads((report / "state.json").read_bytes())
        state["sessions"][settings.gpu].update(
            status="stopped" if outcome.get("gpu") == "stopped" else "stop_unconfirmed",
            runtime_id=str(runtime_id),
            ownership_released=Colab(project).receipt() is None,
            stopped_at=outcome.get("finished_at"),
        )
        atomic_json(report / "state.json", state)


def generate(report: Path, group: str, model: str | None = None) -> None:
    """Reserve once, reconcile recorded IDs and never replay uncertain acceptance."""
    _, config, jobs = load(report)
    api = Client(report / "workspace")
    project = Project(api.workspace)
    for job in jobs:
        if job.group != group or (model and not job.profile.startswith(model)):
            continue
        with FileLock(str(report / ".operation.lock"), timeout=0):
            state = json.loads((report / "state.json").read_bytes())
            matches = [run for run in project.repository.load() if job.source in run.input_sources]
            if len(matches) > 1:
                raise ValueError("Multiple matching requests need explicit reconciliation.")
            if matches:
                if matches[0].state != "saved":
                    raise ValueError("Collect the recorded request without resubmitting it.")
                state["jobs"][job.id] = {"status": "saved", "run_id": str(matches[0].run_id)}
                atomic_json(report / "state.json", state)
                continue
            if job.id in state["jobs"]:
                record = state["jobs"][job.id]
                if record.get("interface") in {"app", "graph"} and record.get("status") == "saved":
                    saved_candidate(project, api, job, record)
                    continue
                raise ValueError("A reserved request has unknown acceptance; do not resubmit it.")
            if len(state["jobs"]) >= config.output_limit:
                raise ValueError("The image budget is exhausted.")
            size = sum(
                path.stat().st_size
                for path in report.rglob("*")
                if path.is_file() and not path.is_relative_to(project.models)
            )
            if size >= config.storage_bytes:
                raise ValueError("The verification storage budget is exhausted.")
            profile = project.settings().presets[job.profile].configured_parameters(job.parameters)
            if preset_fingerprint(profile) != job.preset_sha256:
                raise ValueError("The frozen generation conditions changed.")
            state["jobs"][job.id] = {"status": "reserved", "source_sha256": job.source.sha256}
            atomic_json(report / "state.json", state)
        started = time.monotonic()
        run = api.images.generate(
            job.prompt,
            profile=job.profile,
            seed=job.seed,
            subjects=job.subjects,
            inputs=job.inputs,
            parameters=job.parameters,
            candidates=1,
            input_sources=(job.source,),
        )
        with FileLock(str(report / ".operation.lock"), timeout=0):
            state = json.loads((report / "state.json").read_bytes())
            state["jobs"][job.id] = {
                "status": run.state,
                "run_id": str(run.run_id),
                "seconds": time.monotonic() - started,
            }
            atomic_json(report / "state.json", state)
        print(json.dumps({"job": job.id, "status": run.state}), flush=True)


def expected_pairs(jobs):
    pairs = []
    for first, second in combinations(jobs, 2):
        same_scene = (first.case, first.scene, first.seed) == (
            second.case,
            second.scene,
            second.seed,
        )
        family_first = first.profile.removesuffix("-edit")
        family_second = second.profile.removesuffix("-edit")
        kind = None
        if (
            same_scene
            and first.group == second.group == "baseline"
            and family_first != family_second
        ):
            kind = "model"
        elif (
            same_scene
            and {first.group, second.group} == {"baseline", "ablation"}
            and family_first == family_second
        ):
            kind = "lora"
        elif (
            same_scene
            and first.group == second.group == "edit"
            and first.parent_sha256 == second.parent_sha256
        ):
            if family_first != family_second and first.variant == second.variant:
                kind = "edit_model"
            elif family_first == family_second and first.variant != second.variant:
                kind = "edit_instruction"
        if kind:
            pairs.append({"kind": kind, "first": first.id, "second": second.id})
    return pairs


def summarize(report: Path) -> None:
    frozen, config, jobs = load(report)
    api = Client(report / "workspace")
    project = Project(api.workspace)
    state = json.loads((report / "state.json").read_bytes())
    counts = Counter(row["status"] for row in state["jobs"].values())
    available = {}
    invalid_images = []
    for job in jobs:
        record = state["jobs"].get(job.id, {})
        if record.get("status") != "saved":
            continue
        try:
            available[job.id] = saved_candidate(project, api, job, record)
        except (OSError, ValueError, KomaquillError, KeyError):
            invalid_images.append(job.id)
    try:
        evaluations = sorted(api.evaluations.list(), key=lambda row: row.created_at)
        evaluation_failure = None
    except (OSError, ValueError, KomaquillError) as error:
        evaluations = []
        evaluation_failure = failure_info(error, "evaluation").model_dump(mode="json")
    reviewed = {}
    invalid_evaluations = []
    for job in jobs:
        if job.id not in available:
            continue
        run, name, _, _ = available[job.id]
        for row in evaluations:
            if row.criterion != IMAGE_CRITERION or row.case_id != job.id:
                continue
            try:
                evidence = json.loads(row.reason)
                review = Review.model_validate(evidence["review"])
                if (
                    row.evaluator.kind != "ai"
                    or row.criterion_version != "1"
                    or (review.run_id, review.scene, review.image, review.decision)
                    != (run.run_id, job.scene, name, row.decision)
                    or evidence["conditions_sha256"] != digest(frozen["conditions"][job.case])
                    or evidence["image_sha256"] != run.image_metadata[name].sha256
                ):
                    raise ValueError("The review evidence does not match the candidate.")
                reviewed[job.id] = (row, review)
            except (ValueError, TypeError, KeyError):
                invalid_evaluations.append(str(row.evaluation_id))
    pair_rows = expected_pairs(jobs)
    for pair in pair_rows:
        first = available.get(pair["first"])
        second = available.get(pair["second"])
        pair["result"] = "unrun" if first is None or second is None else "unreviewed"
        if first is None or second is None:
            continue
        for row in evaluations:
            if (
                row.criterion == PAIR_CRITERION
                and row.criterion_version == "1"
                and row.evaluator.kind == "ai"
                and (row.run_id, row.comparison_run_id, row.image, row.comparison_image)
                == (first[0].run_id, second[0].run_id, first[1], second[1])
            ):
                pair["result"] = row.preference
                pair["evaluation_id"] = str(row.evaluation_id)
    works = []
    for model in config.models:
        for case in config.cases:
            for seed in config.seeds:
                selected = [
                    job
                    for job in jobs
                    if (job.group, job.profile, job.case, job.seed - job.scene + 1)
                    == ("baseline", model, case, seed)
                ]
                complete = all(job.id in available for job in selected)
                accepted = complete and all(
                    job.id in reviewed and reviewed[job.id][1].decision == "accept"
                    for job in selected
                )
                works.append(
                    {
                        "model": model,
                        "case": case,
                        "seed": seed,
                        "saved": complete,
                        "quality_approved": accepted,
                    }
                )
                if complete:
                    panels = [
                        {
                            "scene": job.scene,
                            "key": available[job.id][2],
                            "path": available[job.id][3],
                            "text": frozen["plans"][case]["scenes"][job.scene - 1]["narrative"],
                        }
                        for job in selected
                    ]
                    render_page(
                        report / "comics" / f"{case}-{model}-{seed}",
                        f"{case} {model} {seed}",
                        panels,
                        expected_scenes=4,
                    )
    image_rows = []
    for job in jobs:
        item = available.get(job.id)
        if item is None:
            continue
        run, name, key, _ = item
        grade = reviewed.get(job.id)
        image_rows.append(
            {
                "job": job.id,
                "run_id": str(run.run_id),
                "key": key,
                "sha256": run.image_metadata[name].sha256,
                "selected_by": "first_valid_saved_image",
                "quality_decision": grade[1].decision if grade else None,
                "checks": [
                    {"id": f"{job.case}:scene:{job.scene}:{check.criterion}", **check.model_dump()}
                    for check in grade[1].checks
                ]
                if grade
                else [],
                "queue_seconds": (run.generation_started_at - run.created_at).total_seconds()
                if run.generation_started_at
                else None,
                "generation_seconds": (
                    run.generation_completed_at - run.generation_started_at
                ).total_seconds()
                if run.generation_started_at and run.generation_completed_at
                else None,
                "collection_seconds": (run.saved_at - run.generation_completed_at).total_seconds()
                if run.saved_at and run.generation_completed_at
                else None,
            }
        )
    complete = (
        len(available) == len(jobs)
        and len(reviewed) == len(jobs)
        and all(row["result"] not in {"unrun", "unreviewed"} for row in pair_rows)
    )
    atomic_json(
        report / "summary.json",
        {
            "format_version": 1,
            "planned_images": len(jobs),
            "image_budget": config.output_limit,
            "technical_retry_budget": config.technical_retry_limit,
            "reserved_images": len(state["jobs"]),
            "saved_images": counts["saved"],
            "valid_saved_images": len(available),
            "unrun_images": len(jobs) - len(state["jobs"]),
            "quality_reviewed_images": len(reviewed),
            "quality_approved_images": sum(
                review.decision == "accept" for _, review in reviewed.values()
            )
            if reviewed
            else None,
            "comparison_complete": complete,
            "quality_status": "reviewed"
            if available and len(reviewed) == len(available)
            else "requires_image_review",
            "counts": dict(counts),
            "works": works,
            "images": image_rows,
            "pairs": pair_rows,
            "pair_counts": dict(Counter(row["result"] for row in pair_rows)),
            "invalid_image_job_ids": invalid_images,
            "invalid_evaluation_ids": invalid_evaluations,
            "evaluation_failure": evaluation_failure,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "prepare",
            "start",
            "generate",
            "stop",
            "summarize",
            "export-ui",
            "adopt-ui",
            "reviews",
        ],
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--parents", type=Path)
    parser.add_argument("--gpu", choices=["T4", "A100"])
    parser.add_argument("--group", choices=["baseline", "edit", "ablation", "regression"])
    parser.add_argument("--model")
    parser.add_argument("--job")
    parser.add_argument("--view", choices=["app", "graph"], default="app")
    parser.add_argument("--review-file", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.report, args.source, args.parents)
    elif args.action == "start":
        start(args.report, args.source, args.gpu)
    elif args.action == "generate":
        generate(args.report, args.group, args.model)
    elif args.action == "stop":
        stop(args.report)
    elif args.action == "export-ui":
        print(export_ui(args.report, args.job, args.view))
    elif args.action == "adopt-ui":
        adopt_ui(args.report, args.job)
    elif args.action == "reviews":
        record_reviews(args.report, args.review_file)
    else:
        summarize(args.report)


if __name__ == "__main__":
    main()
