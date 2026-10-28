"""Complete frozen story plans with bounded, attributable visual improvements."""

import argparse
import hashlib
import json
import os
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from unittest.mock import patch
from uuid import UUID, uuid4

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, field_validator

from komaquill.api import Client, RenderConfig, SceneRenderConfig
from komaquill.domain.images import PromptSource
from komaquill.domain.materials import InputArtifact
from komaquill.domain.prompts import joined
from komaquill.domain.workflow import digest
from komaquill.infrastructure.atomic import atomic_bytes, atomic_json
from komaquill.infrastructure.colab import Colab
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.monitor_state import read_monitor
from komaquill.infrastructure.stories.storage import FileStoryRepository
from komaquill.infrastructure.workflows import assets
from komaquill.interfaces.stories import present
from tests.live import verification_budget
from tests.support.suite_config import SuiteConfig, defaults

EDIT_PROFILE = "klein-4b-edit"


class Improvement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    scene: int = Field(ge=1, le=8)
    parent_key: str
    method: Literal["regenerate", "edit"]
    prompt: str = Field(min_length=1)
    subjects: tuple[str, ...] = ()
    seed: int
    inputs: dict[str, InputArtifact] = Field(default_factory=dict)
    source: PromptSource
    run_id: UUID | None = None


class ImprovementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: int = Field(ge=1, le=8, strict=True)
    parent_key: str
    method: Literal["regenerate", "edit"]
    prompt: str = Field(min_length=1)
    subjects: tuple[str, ...] = ()


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[3] = 3
    case_id: UUID = Field(default_factory=uuid4)
    story_id: UUID
    prepared_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_plan_sha256: str
    identity: dict[str, str | None]
    frozen_files: dict[str, str]
    baseline_source: PromptSource
    gpu_started_at: datetime | None = None
    runtime_id: UUID | None = None
    submission_deadline: datetime | None = None
    stop_deadline: datetime | None = None
    reserved_outputs: int = 0
    baseline_reserved: bool = False
    baseline_render_id: UUID | None = None
    retry_attempted: bool = False
    improvements: list[Improvement] = Field(default_factory=list)
    selections: dict[int, str] = Field(default_factory=dict)
    config: SuiteConfig = Field(default_factory=defaults)
    scene_count: int = Field(ge=1, le=8)
    runner_sha256: str = ""

    @field_validator("gpu_started_at")
    @classmethod
    def aware_start(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("The GPU budget requires an unambiguous start timestamp.")
        return value


def project(report: Path) -> Project:
    owner = json.loads((report / ".owner.json").read_bytes())
    if owner.get("owner") != "story-completion":
        raise ValueError("This folder does not own a story verification workspace.")
    return Project(Path(owner["workspace"]))


def save(report: Path, case: Case) -> None:
    atomic_json(report / "case.json", case.model_dump(mode="json"))


def load(report: Path) -> Case:
    case = Case.model_validate_json((report / "case.json").read_bytes())
    for name, expected in case.frozen_files.items():
        workspace = name.startswith("@workspace/")
        base = project(report).root.resolve() if workspace else report.resolve()
        relative = name.removeprefix("@workspace/") if workspace else name
        path = (base / relative).resolve()
        if (
            not path.is_relative_to(base)
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise RuntimeError("Frozen case inputs have changed; create a new case.")
    return case


def reserve(case: Case, outputs: int) -> None:
    if outputs < 1 or case.reserved_outputs + outputs > case.config.output_limit:
        raise RuntimeError("The primary output budget is exhausted.")
    case.reserved_outputs += outputs


def sending_allowed(case: Case, *, now: datetime | None = None) -> None:
    started_at, send_seconds = case.gpu_started_at, case.config.send_seconds
    if started_at is None:
        raise RuntimeError("Start the dedicated case explicitly before rendering.")
    deadline = case.submission_deadline or started_at + timedelta(seconds=send_seconds)
    if (now or datetime.now(UTC)) >= deadline:
        raise RuntimeError("The submission deadline has been reached; collect and stop.")


def prepare(
    source_workspace: Path,
    story_id: UUID,
    report: Path,
    *,
    workspace: Path | None = None,
    config: SuiteConfig | None = None,
    model_workspace: Path | None = None,
) -> Case:
    """Copy immutable planning evidence, without creating a new planning result."""
    source = Project(source_workspace)
    model_source = Project(model_workspace) if model_workspace else source
    repository = FileStoryRepository(source)
    plan = repository.read(story_id)
    report.mkdir(parents=True, exist_ok=False)
    config = config or defaults()
    destination = Project(workspace or report / "workspace")
    atomic_json(
        report / ".owner.json",
        {
            "purpose": "bounded story verification",
            "owner": "story-completion",
            "workspace": str(destination.root.resolve()),
            "preserve": True,
        },
    )
    fresh = not (destination.root / "komaquill.toml").exists()
    if fresh:
        Client.initialize(destination.root)
    target_repository = FileStoryRepository(destination)
    target_repository.begin(plan.request)
    origin = repository.folder(story_id)
    target = target_repository.folder(story_id)
    paths = [
        origin / name
        for name in (
            "request.json",
            "source.txt",
            "plan.json",
            "provenance.json",
            "planning-result.json",
        )
    ]
    paths += sorted((origin / "planning").rglob("*.json"))
    for path in paths:
        if path.is_file():
            atomic_bytes(target / path.relative_to(origin), path.read_bytes())
    assert target_repository.read(story_id) == plan
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    if fresh:
        atomic_bytes(
            destination.root / "komaquill.toml",
            (
                f'models = ["klein-4b"]\nlocal_port = {port}\n'
                f"submission_timeout = {config.send_seconds}\n"
                f"session_timeout = {config.total_seconds}\n"
                + "".join(
                    f"\n[assets.{name}]\n"
                    + "".join(
                        f"{key} = {json.dumps(value)}\n"
                        for key, value in model.model_dump(mode="json", exclude_none=True).items()
                    )
                    for model in assets(model_source.settings())
                    for name in (model.name,)
                )
            ).encode(),
        )
    destination.settings()
    render_config = RenderConfig(
        scenes=tuple(
            SceneRenderConfig(scene=scene.position, model="klein-4b") for scene in plan.scenes
        )
    )
    case_id = uuid4()
    recipe = {
        "case_id": str(case_id),
        "story_id": str(story_id),
        "seed": config.seed,
        "candidates": config.candidates,
        "render_config": render_config.model_dump(mode="json"),
    }
    content = json.dumps(recipe, sort_keys=True, ensure_ascii=False)
    baseline_source = PromptSource(
        name="baseline-input.json",
        role="input",
        content=content,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
    )
    atomic_bytes(report / baseline_source.name, content.encode())
    frozen = [p for p in target.rglob("*.json") if p.is_file()]
    frozen += [target / "source.txt", destination.root / "komaquill.toml"]
    frozen += list((destination.root / "presets").rglob("*.*"))
    frozen += [report / baseline_source.name]
    case = Case(
        case_id=case_id,
        story_id=story_id,
        source_plan_sha256=json.loads((origin / "plan.json").read_bytes())["sha256"],
        identity=Project(Path(__file__).resolve().parents[2]).identity(),
        runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        baseline_source=baseline_source,
        config=config,
        scene_count=len(plan.scenes),
        frozen_files={
            (
                "@workspace/" + p.relative_to(destination.root).as_posix()
                if p.is_relative_to(destination.root)
                else p.relative_to(report).as_posix()
            ): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in frozen
        },
    )
    save(report, case)
    return case


def checkpoint_runtime(report: Path) -> None:
    """Retain the owned identity before model transfer or environment preparation finishes."""
    monitor = project(report).state / "monitor.json"
    if not monitor.is_file():
        return
    data = json.loads(monitor.read_bytes())
    if not data.get("runtime_id"):
        return
    runtime_id = UUID(data["runtime_id"])
    with FileLock(str(report / ".case.lock")):
        latest = load(report)
        current = read_monitor(project(report))
        if (
            current is None
            or current.runtime_id != runtime_id
            or (
                latest.runtime_id not in {None, runtime_id}
                and not verification_budget.path(project(report).root).exists()
            )
        ):
            raise RuntimeError("The current owned runtime could not be established.")
        ledger_path = verification_budget.path(project(report).root)
        if latest.runtime_id not in {None, runtime_id} and ledger_path.exists():
            budget = verification_budget.read(project(report).root)
            active = [row for row in budget.allocations if not row.ownership_released]
            if (
                len(active) != 1
                or active[0].runtime_id not in {None, str(runtime_id)}
                or not any(
                    row.runtime_id == str(latest.runtime_id) and row.ownership_released
                    for row in budget.allocations
                )
            ):
                raise RuntimeError("The preceding runtime shutdown is not confirmed.")
        receipt = Colab(project(report)).receipt()
        latest.gpu_started_at = (
            receipt.started_at if receipt and receipt.started_at else current.created_at
        )
        latest.runtime_id = runtime_id
        save(report, latest)


def start(report: Path) -> None:
    with FileLock(str(report / ".case.lock")):
        case = load(report)
        owner = project(report)
        receipt = Colab(owner).receipt()
        if case.gpu_started_at is not None and not verification_budget.path(owner.root).exists():
            raise RuntimeError(
                "Initialize a cumulative verification budget before another startup."
            )
        ledger_key = None
        environment = {}
        if verification_budget.path(owner.root).exists():
            budget = verification_budget.read(owner.root)
            active = [row for row in budget.allocations if not row.ownership_released]
            if receipt and (
                not active or active[0].runtime_id not in {None, str(receipt.runtime_id)}
            ):
                raise RuntimeError("The current runtime ownership is unknown.")
            ledger_key = active[0].key if active else str(uuid4())
            if budget.opening_balance is None:
                raise RuntimeError("Reconcile the opening account before GPU startup.")
            send, stop = verification_budget.begin(owner.root, ledger_key)
            started = verification_budget.read(owner.root).allocations[-1].started_at
            if receipt is None:
                case.submission_deadline = datetime.now(UTC) + timedelta(seconds=send)
                case.stop_deadline = datetime.now(UTC) + timedelta(seconds=stop)
            else:
                send = int((case.submission_deadline - started).total_seconds())
                stop = int((case.stop_deadline - started).total_seconds())
            environment = {
                "KOMAQUILL_SUBMISSION_TIMEOUT": str(send),
                "KOMAQUILL_SESSION_TIMEOUT": str(stop),
                "KOMAQUILL_COMPUTE_UNIT_LIMIT": str(budget.runtime_compute_unit_limit),
                "KOMAQUILL_COMPUTE_UNIT_RESERVE": str(budget.compute_unit_reserve),
            }
        case.gpu_started_at = datetime.now(UTC) if receipt is None else case.gpu_started_at
        save(report, case)
    try:

        def progress(text):
            print(text, flush=True)
            checkpoint_runtime(report)

        with patch.dict(os.environ, environment):
            Client(project(report).root).sessions.start(progress=progress)
    finally:
        primary = sys.exc_info()[1]
        try:
            checkpoint_runtime(report)
            if ledger_key:
                verification_budget.checkpoint(
                    owner.root,
                    ledger_key,
                    runtime_id=str(load(report).runtime_id) if load(report).runtime_id else None,
                )
        except BaseException as secondary:
            if primary is None:
                raise
            primary.add_note("Runtime checkpoint did not complete: " + type(secondary).__name__)


def baseline(report: Path, *, retry_failed: bool = False) -> None:
    """Use the shared CLI presentation and reconcile a previously created rendering."""
    with FileLock(str(report / ".case.lock"), timeout=0):
        case = load(report)
        sending_allowed(case)
        api = Client(project(report).root)
        repository = FileStoryRepository(project(report))
        existing = [
            r for r in repository.renders(case.story_id) if case.baseline_source in r.input_sources
        ]
        if len(existing) > 1:
            raise RuntimeError("Multiple baseline renderings cannot be safely resubmitted.")
        if existing:
            case.baseline_render_id = existing[0].render_id
        elif case.baseline_reserved and case.baseline_render_id is None:
            raise RuntimeError("The reserved baseline identity is unresolved; do not resubmit.")
        if not case.baseline_reserved:
            if verification_budget.path(
                project(report).root
            ).exists() and not verification_budget.reserve(
                project(report).root,
                "baseline:" + str(case.case_id),
                case.scene_count * case.config.candidates,
            ):
                raise RuntimeError("Reconcile the existing baseline reservation first.")
            reserve(case, case.scene_count * case.config.candidates)
            case.baseline_reserved = True
        if retry_failed:
            if case.retry_attempted or case.baseline_render_id is None:
                raise RuntimeError("Only one retry of confirmed failed scenes is allowed.")
            snapshot = api.stories.show(case.story_id, render_id=case.baseline_render_id)
            failed = sum(scene.state in {"failed", "lost"} for scene in snapshot.scenes)
            if failed > case.config.technical_retry_limit:
                raise RuntimeError("The technical retry budget cannot cover every failed scene.")
            if not failed:
                raise RuntimeError("No confirmed failed scenes are available for retry.")
            if verification_budget.path(
                project(report).root
            ).exists() and not verification_budget.reserve(
                project(report).root,
                "retry:" + str(case.case_id),
                failed * case.config.candidates,
            ):
                raise RuntimeError("Reconcile the existing retry reservation first.")
            reserve(case, failed * case.config.candidates)
            case.retry_attempted = True
        save(report, case)
        recipe = json.loads(case.baseline_source.content)
        events = (
            api.stories.resume(case.baseline_render_id, retry_failed=retry_failed)
            if case.baseline_render_id
            else api.stories.render(
                case.story_id,
                render_config=RenderConfig.model_validate(recipe["render_config"]),
                seed=recipe["seed"],
                candidates=case.config.candidates,
                input_sources=(case.baseline_source,),
            )
        )

        def observed():
            while True:
                sending_allowed(case)
                try:
                    event = next(events)
                except StopIteration:
                    return
                case.baseline_render_id = event.snapshot.render_id
                save(report, case)
                yield event

        present(api, observed(), json_output=False)


def improve(report: Path, request_path: Path) -> None:
    """Reserve once before submission; recover request identity from retained input."""
    with FileLock(str(report / ".case.lock"), timeout=0):
        case = load(report)
        api = Client(project(report).root)
        request = ImprovementRequest.model_validate_json(request_path.read_bytes())
        data = request.model_dump(mode="json")
        request_sha = digest(data)
        existing = next(
            (
                j
                for j in case.improvements
                if json.loads(j.source.content)["request_sha256"] == request_sha
            ),
            None,
        )
        if existing:
            reconcile_job(report, case, existing)
            return
        sending_allowed(case)
        if len(case.improvements) >= case.config.improvement_limit:
            raise RuntimeError("The improvement budget is exhausted for this scene.")
        snapshot = api.stories.show(case.story_id, render_id=case.baseline_render_id)
        if not snapshot.complete:
            raise RuntimeError("Finish all baseline scenes before improving artwork.")
        if request.scene > case.scene_count:
            raise ValueError("The improvement scene does not belong to this plan.")
        scene = snapshot.scenes[data["scene"] - 1]
        parents = {image.key for image in scene.candidates}
        for job in case.improvements:
            if job.scene == request.scene and job.run_id is not None:
                run = project(report).repository.read(job.run_id)
                parents.update(
                    f"runs/{run.run_id}/{name}"
                    for name in run.images
                    if run.image_metadata.get(name) and run.image_metadata[name].role == "candidate"
                )
        if request.parent_key not in parents:
            raise ValueError("The parent must be a saved candidate from this scene.")
        inputs = (
            {"reference": api.inputs.import_file(api.artifact_path(request.parent_key))}
            if request.method == "edit"
            else {}
        )
        from komaquill.domain.images import validate_inputs

        profile = EDIT_PROFILE if data["method"] == "edit" else "klein-4b"
        validate_inputs(project(report).settings().presets[profile], inputs)
        job_id = uuid4()
        content = json.dumps(
            {
                "job_id": str(job_id),
                "request_sha256": request_sha,
                "request": data,
                "inputs": {name: item.model_dump(mode="json") for name, item in inputs.items()},
            },
            sort_keys=True,
        )
        recipe = json.loads(case.baseline_source.content)
        job = Improvement(
            job_id=job_id,
            scene=data["scene"],
            parent_key=data["parent_key"],
            method=data["method"],
            prompt=data["prompt"],
            subjects=tuple(data.get("subjects", ())),
            seed=recipe["seed"] + data["scene"] - 1,
            inputs=inputs,
            source=PromptSource(
                name=f"improvement-{job_id}.json",
                role="input",
                content=content,
                sha256=hashlib.sha256(content.encode()).hexdigest(),
            ),
        )
        if verification_budget.path(
            project(report).root
        ).exists() and not verification_budget.reserve(
            project(report).root,
            "improvement:" + str(case.case_id) + ":" + request_sha,
            case.config.candidates,
        ):
            raise RuntimeError("Reconcile the existing improvement reservation first.")
        reserve(case, case.config.candidates)
        case.improvements.append(job)
        save(report, case)
        atomic_bytes(report / job.source.name, content.encode())
        try:
            run = api.images.generate(
                joined(recipe["render_config"]["common_prompt"], job.prompt),
                subjects=job.subjects,
                seed=job.seed,
                model="klein-4b",
                operation="edit" if profile == EDIT_PROFILE else "generate",
                candidates=case.config.candidates,
                inputs=job.inputs,
                input_sources=(job.source,),
            )
            job.run_id = run.run_id
        except BaseException as error:
            try:
                reconcile_job(report, case, job)
            except Exception as checkpoint_error:
                error.add_note(f"Checkpoint failure: {type(checkpoint_error).__name__}")
            raise
        else:
            reconcile_job(report, case, job)


def reconcile_job(report: Path, case: Case, job: Improvement) -> None:
    matches = [run for run in project(report).repository.load() if job.source in run.input_sources]
    if len(matches) > 1:
        raise RuntimeError("Multiple matching improvement requests require reconciliation.")
    if matches:
        job.run_id = matches[0].run_id
    save(report, case)
    print(
        json.dumps(
            {
                "job_id": str(job.job_id),
                "run_id": str(job.run_id) if job.run_id else None,
                "state": matches[0].state if matches else "unresolved",
            }
        )
    )


def stop(report: Path) -> None:
    owner = json.loads((report / ".owner.json").read_bytes())
    if owner.get("owner") != "story-completion":
        raise RuntimeError("This folder is not a completion verification workspace.")
    try:
        case = Case.model_validate_json((report / "case.json").read_bytes())
        runtime_id = case.runtime_id
        started_at, total_seconds = case.gpu_started_at, case.config.total_seconds
        elapsed = (datetime.now(UTC) - started_at).total_seconds() if started_at else total_seconds
        force = (
            datetime.now(UTC) >= case.stop_deadline
            if case.stop_deadline
            else elapsed >= total_seconds
        )
    except (OSError, ValueError):
        force = True
        runtime_id = None
    Client(project(report).root).sessions.stop(force=force, progress=print)
    root = project(report).logs
    path = root / str(runtime_id) / "shutdown.json"
    if runtime_id is not None and path.is_file():
        outcome = json.loads(path.read_bytes())
        atomic_json(report / "shutdown.json", outcome)
        owner = project(report)
        if verification_budget.path(owner.root).exists():
            budget = verification_budget.read(owner.root)
            active = [row for row in budget.allocations if not row.ownership_released]
            if active:
                verification_budget.checkpoint(
                    owner.root,
                    active[0].key,
                    runtime_id=str(runtime_id),
                    stopped_at=datetime.fromisoformat(outcome["finished_at"])
                    if outcome.get("gpu") == "stopped"
                    else None,
                    released=outcome.get("gpu") == "stopped" and Colab(owner).receipt() is None,
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("prepare", "start", "baseline", "improve", "report", "stop")
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--source-workspace", type=Path)
    parser.add_argument("--story-id", type=UUID)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--allow-gpu", action="store_true")
    args = parser.parse_args()
    if args.action in {"start", "baseline", "improve", "stop"} and not args.allow_gpu:
        parser.error("Use --allow-gpu only for an explicit live operation.")
    if args.action == "prepare":
        if not args.source_workspace or not args.story_id:
            parser.error("Prepare requires --source-workspace and --story-id.")
        prepare(args.source_workspace, args.story_id, args.report)
    elif args.action == "start":
        start(args.report)
    elif args.action == "baseline":
        baseline(args.report, retry_failed=args.retry_failed)
    elif args.action == "improve":
        if not args.request:
            parser.error("Improve requires --request.")
        improve(args.report, args.request)
    elif args.action == "report":
        from tests.live.completion_report import publish

        publish(args.report)
    else:
        stop(args.report)
