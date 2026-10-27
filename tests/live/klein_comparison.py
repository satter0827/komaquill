"""Compare frozen Klein configurations without calling a planning model."""

import argparse
import hashlib
import json
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
from komaquill.infrastructure.asset_store import file_digest
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.colab import Colab
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.monitor_state import read_monitor
from komaquill.infrastructure.sessions.connection import connected
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
    _, config, jobs = load(report, executing=True)
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
    conditions = review_conditions(report, frozen)
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
                "conditions_sha256": digest(conditions[job.case]),
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
    group: Literal["baseline", "edit", "ablation"]
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
    data = json.loads(CONFIG.with_name("specifications.json").read_bytes())[case_id]
    for character in data["characters"]:
        character["appearance"] = config.presentation.get(character["key"], character["appearance"])
    for prop in data["props"]:
        prop["appearance"] = config.presentation.get(prop["key"], prop["appearance"])
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


def scene_conditions(case_id: str, scene: Scene) -> dict:
    """Bind each expectation to its own frozen scene and source references."""
    return {
        "source_refs": scene.source_refs,
        "conditions": {
            f"{case_id}:scene:{scene.position}:content": scene.visual_description,
            **{
                f"{case_id}:scene:{scene.position}:subject:{subject.character}": subject.action
                for subject in scene.subjects
            },
            **{
                f"{case_id}:scene:{scene.position}:prop:{state.prop}": {
                    "visible": state.visible,
                    "holder": state.holder,
                    "state": state.state,
                }
                for state in scene.props
            },
        },
    }


def review_conditions(report: Path, frozen: dict) -> dict:
    """Apply an explicit append-only correction without changing frozen generation data."""
    result = frozen["conditions"]
    correction = report / "condition-correction.json"
    if correction.exists():
        data = json.loads(correction.read_bytes())
        if (
            data.get("format_version") != 1
            or data.get("comparison_sha256") != digest(frozen)
            or data.get("original_conditions_sha256") != digest(result)
            or not data.get("reason")
        ):
            raise ValueError("The condition correction does not identify this comparison.")
        result = data["conditions"]
    for key, value in frozen["plans"].items():
        plan = StoryPlan.model_validate(value)
        expected = {str(scene.position): scene_conditions(key, scene) for scene in plan.scenes}
        if result[key]["scenes"] != expected or any(
            result[key][name] != frozen["conditions"][key][name]
            for name in ("source_sha256", "source", "presentation")
        ):
            raise ValueError("The criteria do not match their frozen source and scene.")
    return result


def prepare(report: Path, parents_workspace: Path, parents_file: Path) -> None:
    """Freeze every job, parent and expectation before any GPU is requested."""
    if report.exists():
        raise ValueError("Use the existing frozen comparison instead of overwriting it.")
    config = configuration()
    client = Client.initialize(report / "workspace")
    project = Project(client.workspace)
    settings_file = project.root / "komaquill.toml"
    settings_file.write_text(
        settings_file.read_text(encoding="utf-8").replace(
            'models = ["klein-4b"]', "models = " + json.dumps(config.models)
        ),
        encoding="utf-8",
    )
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
        for scene in plans["red-book"].scenes[:1] if model == "wai-illustrious" else []:
            compiled = scene_input(plans["red-book"], scene, profile)
            parameters = {"detail_strength": 0.0}
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
    for index, parent in enumerate(parents[:1], 1):
        path = origin.artifact_path(parent["key"])
        artifact = client.inputs.import_file(path)
        if artifact.sha256 != parent["sha256"]:
            raise ValueError("A common edit parent changed.")
        for model in config.models:
            profile_name = (
                model + "-edit" if model.startswith("klein-") else "wai-illustrious-img2img"
            )
            for variant in ("visible_state",):
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
    counts = Counter(job.group for job in jobs)
    if counts != {
        "baseline": config.baseline_limit,
        "edit": config.edit_limit,
        "ablation": config.ablation_limit,
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
                    "scenes": {
                        str(scene.position): {
                            **scene_conditions(key, scene),
                        }
                        for scene in plans[key].scenes
                    },
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


class ExecutionRevision(BaseModel):
    """Record an explicit recovery target without changing frozen image conditions."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1] = 1
    comparison_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity: dict[str, object]
    runner_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_limit: int = Field(ge=1, strict=True)
    created_at: datetime
    reason: str = Field(min_length=1)


def execution_target(report: Path, frozen) -> dict:
    """Keep recovery approvals separate from the immutable comparison and budgets."""
    result = {
        "identity": frozen["identity"],
        "runner_sha256": frozen["runner_sha256"],
        "session_limit": 1,
    }
    path = report / "execution-revisions.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            revision = ExecutionRevision.model_validate_json(line)
            if revision.comparison_sha256 != digest(frozen):
                raise ValueError("The recovery target belongs to another comparison.")
            result = revision.model_dump(mode="json")
    return result


def load(report: Path, *, executing: bool = False):
    frozen = json.loads((report / "comparison.json").read_bytes())
    state = json.loads((report / "state.json").read_bytes())
    if (
        frozen.get("format_version") != 1
        or state.get("format_version") != 1
        or digest(frozen) != state.get("comparison_sha256")
    ):
        raise ValueError("The frozen comparison or its version changed.")
    target = execution_target(report, frozen)
    if (
        executing
        and target["runner_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise ValueError("The comparison runner changed after preparation.")
    config = ComparisonConfig.model_validate(frozen["config"])
    jobs = [Job.model_validate(row) for row in frozen["jobs"]]
    if executing and target["identity"] != Project(report / "workspace").identity():
        raise ValueError("The executable code changed after the comparison was frozen.")
    return frozen, config, jobs


def start(report: Path, gpu: Literal["T4"] = "T4") -> None:
    """Prepare only selected assets and preserve the shared CU baseline."""
    frozen, config, _ = load(report, executing=True)
    project = Project(report / "workspace")
    with FileLock(str(report / ".operation.lock"), timeout=0):
        state = json.loads((report / "state.json").read_bytes())
        receipt = Colab(project).receipt()
        if receipt is None:
            if any(row.get("status") != "stopped" for row in state["sessions"].values()):
                raise ValueError(
                    "Confirm the previous session shutdown before allocating another GPU."
                )
            if len(state["sessions"]) >= execution_target(report, frozen)["session_limit"]:
                raise ValueError("The approved session budget is exhausted.")
            session_key = f"{gpu}-{len(state['sessions']) + 1}"
        else:
            matches = [
                key
                for key, row in state["sessions"].items()
                if row.get("runtime_id") == str(receipt.runtime_id)
            ]
            if len(matches) != 1 or receipt.settings is None or receipt.settings.gpu != gpu:
                raise ValueError("The current session ownership is unknown.")
            session_key = matches[0]
        models = list(config.models)
        values = {
            "models": models,
            "gpu": gpu,
            "high_ram": False,
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
        state["sessions"].setdefault(session_key, {"started_at": datetime.now(UTC).isoformat()})
        state["sessions"][session_key]["status"] = "requested"
        atomic_json(report / "state.json", state)
    try:
        Client(project.root).sessions.start(models=models, gpu=gpu, high_ram=False)
    except BaseException as error:
        with FileLock(str(report / ".operation.lock"), timeout=0):
            state = json.loads((report / "state.json").read_bytes())
            state["sessions"][session_key].setdefault(
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
        state["sessions"][session_key].update(
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
        matches = [
            key
            for key, row in state["sessions"].items()
            if row.get("runtime_id") == str(runtime_id)
        ]
        pending = [
            key for key, row in state["sessions"].items() if row.get("status") == "requested"
        ]
        session_key = matches[0] if len(matches) == 1 else pending[-1] if pending else None
        if session_key is None:
            raise ValueError("The shutdown result has no matching session record.")
        state["sessions"][session_key].update(
            status="stopped" if outcome.get("gpu") == "stopped" else "stop_unconfirmed",
            runtime_id=str(runtime_id),
            ownership_released=Colab(project).receipt() is None,
            stopped_at=outcome.get("finished_at"),
        )
        atomic_json(report / "state.json", state)


def generate(report: Path, group: str, model: str | None = None) -> None:
    """Reserve once, reconcile recorded IDs and never replay uncertain acceptance."""
    _, config, jobs = load(report, executing=True)
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
                if matches[0].state not in {"saved", "failed"}:
                    raise ValueError("Collect the recorded request without resubmitting it.")
                state["jobs"][job.id] = {
                    "status": matches[0].state,
                    "run_id": str(matches[0].run_id),
                }
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
        try:
            run = api.images.generate(
                job.prompt,
                model=project.settings().presets[job.profile].model_id,
                operation=project.settings().presets[job.profile].operation,
                seed=job.seed,
                subjects=job.subjects,
                inputs=job.inputs,
                parameters=job.parameters,
                candidates=1,
                input_sources=(job.source,),
            )
        except KomaquillError as error:
            identity = error.details.get("run_id")
            recorded = project.repository.read(UUID(str(identity))) if identity else None
            if (
                recorded is None
                or recorded.state != "failed"
                or job.source not in recorded.input_sources
            ):
                raise
            run = recorded
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


def scene_approved(frozen, job: Job, review: Review) -> bool:
    """Require scene scope and every frozen obligation before counting quality approval."""
    expected = set(frozen["conditions"][job.case]["scenes"][str(job.scene)]["conditions"])
    matches = {check.criterion for check in review.checks if check.verdict == "match"}
    return review.scope == "scene" and review.decision == "accept" and expected <= matches


def summarize(report: Path) -> None:
    frozen, config, jobs = load(report)
    criteria = {"conditions": review_conditions(report, frozen)}
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
                    or evidence["conditions_sha256"] != digest(criteria["conditions"][job.case])
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
                    job.id in reviewed and scene_approved(criteria, job, reviewed[job.id][1])
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
                        frozen["plans"][case]["title"],
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
                "review_scope": grade[1].scope if grade else None,
                "checks": [
                    {
                        "id": check.criterion
                        if check.criterion
                        in criteria["conditions"][job.case]["scenes"][str(job.scene)]["conditions"]
                        else f"{job.case}:scene:{job.scene}:{check.criterion}",
                        **check.model_dump(),
                    }
                    for check in grade[1].checks
                ]
                if grade
                else [],
                "queue_seconds": (run.generation_started_at - run.created_at).total_seconds()
                if job.source in run.input_sources
                and run.generation_started_at
                and run.generation_started_at >= run.created_at
                else None,
                "queue_observation": "submission_record"
                if job.source in run.input_sources
                else "first_observation_only",
                "generation_seconds": (
                    run.generation_completed_at - run.generation_started_at
                ).total_seconds()
                if run.generation_started_at
                and run.generation_completed_at
                and run.generation_completed_at >= run.generation_started_at
                else None,
                "collection_seconds": (run.saved_at - run.generation_completed_at).total_seconds()
                if run.saved_at
                and run.generation_completed_at
                and run.saved_at >= run.generation_completed_at
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
            "producer": frozen["identity"],
            "execution_target": execution_target(report, frozen),
            "reader": project.identity(),
            "planned_images": len(jobs),
            "image_budget": config.output_limit,
            "technical_retry_budget": config.technical_retry_limit,
            "reserved_images": len(state["jobs"]),
            "saved_images": counts["saved"],
            "valid_saved_images": len(available),
            "unrun_images": len(jobs) - len(state["jobs"]),
            "quality_reviewed_images": len(reviewed),
            "local_approved_images": sum(
                review.scope == "local" and review.decision == "accept"
                for _, review in reviewed.values()
            ),
            "quality_approved_images": sum(
                scene_approved(criteria, next(job for job in jobs if job.id == identity), review)
                for identity, (_, review) in reviewed.items()
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
    parser.add_argument("--gpu", choices=["T4"])
    parser.add_argument("--group", choices=["baseline", "edit", "ablation"])
    parser.add_argument("--model")
    parser.add_argument("--job")
    parser.add_argument("--view", choices=["app", "graph"], default="app")
    parser.add_argument("--review-file", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.report, args.source, args.parents)
    elif args.action == "start":
        start(args.report, args.gpu or "T4")
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
