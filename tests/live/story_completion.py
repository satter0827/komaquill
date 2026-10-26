"""Complete the fixed four-scene story before running bounded visual improvements."""

import argparse
import hashlib
import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, field_validator

from komaquill.api import Client, RenderConfig, SceneRenderConfig
from komaquill.domain.images import PromptSource
from komaquill.domain.materials import InputArtifact
from komaquill.domain.workflow import digest
from komaquill.infrastructure.asset_store import model_path, register, verified_cache
from komaquill.infrastructure.atomic import atomic_bytes, atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.stories.storage import FileStoryRepository
from komaquill.interfaces.stories import present
from tests.live.completion_workflow import PROFILE, prepare_repair

SCENES = 4
CANDIDATES = 2
MAX_OUTPUTS = 12
MAX_IMPROVEMENTS = 2
SEND_SECONDS = 40 * 60
TOTAL_SECONDS = 60 * 60
BASE_SEED = 424242
COMMON_PROMPT = (
    "rain falling in the uncovered street, wet pavement, cool overcast daylight, "
    "soft painted storybook illustration"
)
NEGATIVE = "deformed body, fused bodies, floating objects"
DESCRIPTIONS = (
    (
        "rainy shop street, a young woman crouches beside a small kitten",
        (
            "one human young woman, short black bob, yellow raincoat, holding one blue umbrella",
            "one tiny gray domestic kitten, white chest patch, natural feline body, "
            "on the pavement",
        ),
    ),
    (
        "close view of a small kitten sheltered beneath a blue umbrella, rain beyond the canopy",
        (
            "one human hand in a yellow raincoat sleeve gripping a blue umbrella handle, "
            "only the forearm and hand visible",
            "one tiny gray kitten, white chest patch, sitting safely under the blue umbrella",
        ),
    ),
    (
        "sheltered pavement beneath a shop awning, a kitten has approached its mother, "
        "rain beyond the shelter",
        (
            "one adult gray mother cat, white chest patch, natural feline body, sitting, "
            "facing right",
            "one much smaller gray kitten, white chest patch, natural feline body, standing, "
            "facing left",
        ),
    ),
    (
        "rear view of a young woman walking away along the rainy street, "
        "one blue umbrella above her head",
        (
            "one human young woman, short black bob, yellow raincoat, black boots, back view, "
            "holding one blue umbrella",
        ),
    ),
)


class Improvement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    scene: int = Field(ge=1, le=SCENES)
    parent_key: str
    method: Literal["regenerate", "inpaint"]
    prompt: str = Field(min_length=1)
    negative: str = NEGATIVE
    subjects: tuple[str, ...] = ()
    seed: int
    inputs: dict[str, InputArtifact] = Field(default_factory=dict)
    source: PromptSource
    run_id: UUID | None = None


class ImprovementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: int = Field(ge=1, le=SCENES, strict=True)
    parent_key: str
    method: Literal["regenerate", "inpaint"]
    prompt: str = Field(min_length=1)
    negative: str = NEGATIVE
    subjects: tuple[str, ...] = Field(default=(), max_length=2)
    mask: str | None = None


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1] = 1
    case_id: UUID = Field(default_factory=uuid4)
    story_id: UUID
    prepared_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_plan_sha256: str
    identity: dict[str, str | None]
    frozen_files: dict[str, str]
    baseline_source: PromptSource
    gpu_started_at: datetime | None = None
    runtime_id: UUID | None = None
    reserved_outputs: int = 0
    baseline_reserved: bool = False
    baseline_render_id: UUID | None = None
    retry_attempted: bool = False
    improvements: list[Improvement] = Field(default_factory=list)
    output_limit: int = MAX_OUTPUTS
    improvement_limit: int = MAX_IMPROVEMENTS
    candidates: int = CANDIDATES
    send_seconds: int = SEND_SECONDS
    total_seconds: int = TOTAL_SECONDS
    runner_sha256: str = ""

    @field_validator("gpu_started_at")
    @classmethod
    def aware_start(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("The GPU budget requires an unambiguous start timestamp.")
        return value


def project(report: Path) -> Project:
    return Project(report / "workspace")


def save(report: Path, case: Case) -> None:
    atomic_json(report / "case.json", case.model_dump(mode="json"))


def load(report: Path) -> Case:
    case = Case.model_validate_json((report / "case.json").read_bytes())
    for name, expected in case.frozen_files.items():
        path = (report / name).resolve()
        if (
            not path.is_relative_to(report.resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise RuntimeError("Frozen case inputs have changed; create a new case.")
    return case


def reserve(case: Case, outputs: int) -> None:
    if outputs < 1 or case.reserved_outputs + outputs > case.output_limit:
        raise RuntimeError("The primary output budget is exhausted.")
    case.reserved_outputs += outputs


def sending_allowed(case: Case, *, now: datetime | None = None) -> None:
    if case.gpu_started_at is None:
        raise RuntimeError("Start the dedicated case explicitly before rendering.")
    if ((now or datetime.now(UTC)) - case.gpu_started_at).total_seconds() >= case.send_seconds:
        raise RuntimeError("The submission deadline has been reached; collect and stop.")


def prepare(source_workspace: Path, story_id: UUID, report: Path) -> Case:
    """Copy immutable planning evidence, without creating a new planning result."""
    source = Project(source_workspace)
    repository = FileStoryRepository(source)
    plan = repository.read(story_id)
    if len(plan.scenes) != SCENES:
        raise ValueError("This verification requires the saved four-scene plan.")
    report.mkdir(parents=True, exist_ok=False)
    Client.initialize(report / "workspace")
    destination = project(report)
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
    prepare_repair(destination.root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    atomic_bytes(
        destination.root / "komaquill.toml",
        f'profiles = ["pony-story", "{PROFILE}"]\nlocal_port = {port}\n'.encode(),
    )
    destination.settings()
    model = source.settings().assets["pony"]
    source_model = model_path(source.models, model)
    if not verified_cache(source.models, model):
        from komaquill.infrastructure.artifacts import file_digest

        if file_digest(source_model) != model.sha256:
            raise ValueError("The retained Pony model does not match its pinned checksum.")
    destination_model = model_path(destination.models, model)
    destination_model.parent.mkdir(parents=True)
    os.link(source_model, destination_model)
    register(destination.models, model, destination_model)
    config = RenderConfig(
        common_prompt=COMMON_PROMPT,
        scenes=tuple(
            SceneRenderConfig(
                scene=index,
                profile="pony-story",
                prompt=prompt,
                negative=NEGATIVE,
                subjects=subjects,
            )
            for index, (prompt, subjects) in enumerate(DESCRIPTIONS, 1)
        ),
    )
    case_id = uuid4()
    recipe = {
        "case_id": str(case_id),
        "story_id": str(story_id),
        "seed": BASE_SEED,
        "candidates": CANDIDATES,
        "render_config": config.model_dump(mode="json"),
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
        frozen_files={
            p.relative_to(report).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in frozen
        },
    )
    save(report, case)
    atomic_json(
        report / ".owner.json",
        {
            "purpose": "story completion verification and retained artwork",
            "owner": "story-completion",
            "preserve": True,
        },
    )
    return case


def start(report: Path) -> None:
    with FileLock(str(report / ".case.lock")):
        case = load(report)
        if case.gpu_started_at is not None:
            raise RuntimeError("A GPU startup was already attempted; inspect and stop this case.")
        case.gpu_started_at = datetime.now(UTC)
        save(report, case)
    try:
        Client(project(report).root).sessions.start(progress=print)
    finally:
        monitor = project(report).state / "monitor.json"
        if monitor.is_file():
            data = json.loads(monitor.read_bytes())
            case.runtime_id = UUID(data["runtime_id"]) if data.get("runtime_id") else None
            save(report, case)


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
            raise RuntimeError("Multiple baseline renderings require manual reconciliation.")
        if existing:
            case.baseline_render_id = existing[0].render_id
        elif case.baseline_reserved and case.baseline_render_id is None:
            raise RuntimeError("The reserved baseline identity is unresolved; do not resubmit.")
        if not case.baseline_reserved:
            reserve(case, SCENES * case.candidates)
            case.baseline_reserved = True
        if retry_failed:
            if case.retry_attempted or case.baseline_render_id is None:
                raise RuntimeError("Only one retry of confirmed failed scenes is allowed.")
            snapshot = api.stories.show(case.story_id, render_id=case.baseline_render_id)
            failed = sum(scene.state in {"failed", "lost"} for scene in snapshot.scenes)
            if not failed:
                raise RuntimeError("No confirmed failed scenes are available for retry.")
            reserve(case, failed * case.candidates)
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
                candidates=case.candidates,
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
        if len(case.improvements) >= case.improvement_limit or any(
            j.scene == data["scene"] for j in case.improvements
        ):
            raise RuntimeError("The improvement round is exhausted for this scene.")
        snapshot = api.stories.show(case.story_id, render_id=case.baseline_render_id)
        if not snapshot.complete:
            raise RuntimeError("Finish all four baseline scenes before improving artwork.")
        scene = snapshot.scenes[data["scene"] - 1]
        if data["parent_key"] not in {image.key for image in scene.candidates}:
            raise ValueError("The parent must be a saved baseline candidate from this scene.")
        inputs = {}
        if data["method"] == "inpaint":
            if not request.mask:
                raise ValueError("Local correction requires an edit mask.")
            inputs = {
                "base": api.inputs.import_file(api.artifact_path(data["parent_key"])),
                "mask": api.inputs.import_file(request_path.parent / request.mask, kind="mask"),
            }
        from komaquill.domain.images import validate_inputs

        profile = PROFILE if data["method"] == "inpaint" else "pony-story"
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
            negative=data.get("negative", NEGATIVE),
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
        reserve(case, case.candidates)
        case.improvements.append(job)
        save(report, case)
        atomic_bytes(report / job.source.name, content.encode())
        try:
            run = api.images.generate(
                f"{recipe['render_config']['common_prompt']}, {job.prompt}",
                negative=job.negative,
                subjects=job.subjects,
                seed=job.seed,
                profile=profile,
                candidates=case.candidates,
                inputs=job.inputs,
                input_sources=(job.source,),
                parameters={"denoise": 0.55} if job.method == "inpaint" else None,
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
        elapsed = (
            (datetime.now(UTC) - case.gpu_started_at).total_seconds()
            if case.gpu_started_at
            else case.total_seconds
        )
        force = elapsed >= case.total_seconds
    except (OSError, ValueError):
        force = True
    Client(project(report).root).sessions.stop(force=force, progress=print)
    root = project(report).logs
    paths = list(root.glob("*/shutdown.json"))
    if paths:
        atomic_json(report / "shutdown.json", json.loads(paths[-1].read_bytes()))


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
