"""Measure a bounded story suite using public APIs and retained review decisions."""

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from filelock import FileLock
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from komaquill.api import Client, Evaluator, KomaquillError
from komaquill.infrastructure.atomic import atomic_bytes, atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.stories.storage import FileStoryRepository
from komaquill.infrastructure.workflows import assets
from tests.live import completion_report, story_completion
from tests.support import llm_comparison
from tests.support.story_cases import CORPUS, cases
from tests.support.story_evidence import planning_measurement
from tests.support.suite_config import SuiteConfig, defaults

RESTORE_SECONDS = 180
REQUEST_GUARD_SECONDS = 5
MINIMUM_CONTEXT_TOKENS = 8192


class PlanningBudgetExhausted(RuntimeError):
    """Reject locally before a request would exceed the persisted budget."""


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: int = 1
    source_workspace: Path
    config: SuiteConfig
    identity: dict[str, str | None]
    corpus_sha256: str
    planning_started_at: AwareDatetime | None = None
    planning_calls: StrictInt = Field(default=0, ge=0)
    plans: dict[str, list[dict]] = Field(default_factory=dict)
    members: list[str] = Field(default_factory=list)
    operations: dict[str, StrictInt] = Field(default_factory=dict)
    gpu_started_at: AwareDatetime | None = None
    shutdown_confirmed: bool = False
    stopping: bool = False


class PlanReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    story_id: UUID
    source_sha256: str
    plan_sha256: str
    decision: str = Field(pattern="^(accept|reject|undecided)$")
    reason: str = Field(min_length=1)
    evaluator: Evaluator


def save(report: Path, suite: Suite) -> None:
    atomic_json(report / "suite.json", suite.model_dump(mode="json"))


def load(report: Path) -> Suite:
    suite = Suite.model_validate_json((report / "suite.json").read_bytes())
    corpus = (report / "corpus.json").read_bytes()
    if hashlib.sha256(corpus).hexdigest() != suite.corpus_sha256:
        raise ValueError("The frozen story corpus has changed.")
    known = {row["id"] for row in json.loads(corpus)}
    if (
        not set(suite.config.live_cases) <= known
        or not set(suite.members) <= set(suite.config.live_cases)
        or len(suite.members) != len(set(suite.members))
        or suite.planning_calls > suite.config.planning_calls
        or any(value < 1 for value in suite.operations.values())
        or sum(suite.operations.values()) > suite.config.output_limit
    ):
        raise ValueError("Retained case identities or budget counters are inconsistent.")
    return suite


def initialize(source: Path, report: Path, *, config: SuiteConfig | None = None) -> None:
    report.mkdir(parents=True, exist_ok=False)
    config = config or defaults()
    if not set(config.live_cases) <= cases().keys():
        raise ValueError("Every live case must exist in the paired corpus.")
    corpus = CORPUS.read_bytes()
    atomic_bytes(report / "corpus.json", corpus)
    Client.initialize(report / "planning")
    save(
        report,
        Suite(
            source_workspace=source.resolve(),
            config=config,
            identity=Project(source).identity(),
            corpus_sha256=hashlib.sha256(corpus).hexdigest(),
        ),
    )


def plan(
    report: Path,
    case_id: str,
    executable: Path,
    *,
    llm_profile: str = "lmstudio-4b",
    review_feedback: bool = True,
) -> None:
    """Charge every model call before invocation, including structural repair calls."""
    from komaquill.api import stories

    with FileLock(str(report / ".planning.lock"), timeout=0):
        suite = load(report)
        if case_id not in suite.config.live_cases or suite.gpu_started_at is not None:
            raise ValueError("Planning requires an authorized case before GPU startup.")
        case = next(
            row for row in json.loads((report / "corpus.json").read_bytes()) if row["id"] == case_id
        )
        if suite.planning_started_at is None:
            suite.planning_started_at = datetime.now(UTC)
            save(report, suite)

        def remaining():
            current = load(report)
            return (
                current.config.planning_seconds
                - (datetime.now(UTC) - current.planning_started_at).total_seconds()
            )

        if remaining() <= RESTORE_SECONDS or suite.planning_calls >= suite.config.planning_calls:
            raise RuntimeError("The planning budget is exhausted.")
        original = llm_comparison.loaded(llm_comparison.inventory())
        from komaquill.infrastructure.stories.configuration import settings

        workspace = Project(report / "planning")
        if llm_profile not in {"lmstudio-4b", "lmstudio-9b"}:
            raise ValueError("Select an explicitly authorized planning model.")
        options = settings(workspace, llm_profile=llm_profile, model="klein-4b")
        allowed = {options.llm_profiles[name].model for name in ("lmstudio-4b", "lmstudio-9b")}
        if any(row["key"] not in allowed for row in original):
            raise RuntimeError("An unrelated loaded model prevents controlled measurement.")
        attempt = len(suite.plans.get(case_id, [])) + 1
        folder = report / "planning-measurements" / f"{case_id}-{attempt}"
        folder.mkdir(parents=True, exist_ok=False)
        atomic_json(folder / "original.json", {"models": original})
        factory = stories.planner
        application_plan = stories.plan_story
        semantic_correction = None
        semantic_parent = None

        class BudgetedPlanner:
            def __init__(self, delegate):
                self.delegate = delegate
                self.provenance = delegate.provenance

            def complete(self, request, schema, correction):
                nonlocal story_id
                story_id = request.story_id
                with FileLock(str(report / ".suite.lock")):
                    current = load(report)
                    if (
                        current.planning_calls >= current.config.planning_calls
                        or remaining() <= RESTORE_SECONDS + self.provenance.timeout_seconds
                    ):
                        raise PlanningBudgetExhausted("The planning call budget is exhausted.")
                    current.planning_calls += 1
                    save(report, current)
                with llm_comparison.MemoryTrace(folder / "memory.jsonl"):
                    return self.delegate.complete(request, schema, correction)

        def budgeted(project, selected, *, allow_real_llm):
            selected = selected.model_copy(
                update={
                    "request_timeout": min(
                        selected.request_timeout,
                        max(0.1, remaining() - RESTORE_SECONDS - REQUEST_GUARD_SECONDS),
                    )
                }
            )
            return BudgetedPlanner(factory(project, selected, allow_real_llm=allow_real_llm))

        result = failure = story_id = None
        started_at, before = datetime.now(UTC).isoformat(), time.monotonic()
        config_path = workspace.root / "komaquill.toml"
        original_config = config_path.read_bytes()
        try:
            control = llm_comparison.switch(
                executable,
                options.llm_profiles[llm_profile].model,
                max(MINIMUM_CONTEXT_TOKENS, options.max_output_tokens * 2),
                allowed,
                folder,
            )
            atomic_json(folder / "loaded.json", control)
            reviews_path = report / "planning-reviews.json"
            if review_feedback and reviews_path.is_file():
                previous = [
                    PlanReview.model_validate(row)
                    for row in json.loads(reviews_path.read_bytes())
                    if row["case_id"] == case_id
                    and row["decision"] == "reject"
                    and any(
                        item.get("story_id") == row["story_id"]
                        and item.get("llm_profile") == llm_profile
                        for item in suite.plans.get(case_id, [])
                    )
                ]
                if previous:
                    review = previous[-1]
                    if review.source_sha256 != hashlib.sha256(case["source"].encode()).hexdigest():
                        raise ValueError("Repair feedback refers to a different source.")
                    previous_plan = FileStoryRepository(workspace).read(review.story_id)
                    envelope = json.loads(
                        (
                            FileStoryRepository(workspace).folder(review.story_id) / "plan.json"
                        ).read_bytes()
                    )
                    if (
                        envelope["sha256"] != review.plan_sha256
                        or previous_plan.request.source_sha256 != review.source_sha256
                    ):
                        raise ValueError("The semantic review does not match its retained plan.")
                    evidence = FileStoryRepository(workspace).planning_evidence(review.story_id)
                    responses = [
                        row["response"] for row in evidence["attempts"] if row.get("response")
                    ]
                    if not responses:
                        raise ValueError("The rejected planning response is unavailable.")
                    previous_output = responses[-1]["content"]
                    from komaquill.domain.stories import StoryDraft

                    retained = previous_plan.model_dump(mode="json")
                    retained = {key: retained[key] for key in StoryDraft.model_fields}
                    retained["scenes"] = [
                        {
                            key: value
                            for key, value in scene.items()
                            if key not in {"scene_id", "position", "characters"}
                        }
                        for scene in retained["scenes"]
                    ]
                    if json.loads(previous_output) != retained:
                        raise ValueError("The retained response differs from the reviewed plan.")
                    semantic_correction = json.dumps(
                        {
                            "previous_output": previous_output,
                            "validation_errors": [{"code": "semantic_review", "loc": []}],
                            "requirements": review.reason,
                        },
                        ensure_ascii=False,
                    )
                    semantic_parent = str(review.story_id)
                    atomic_json(
                        folder / "repair-review.json",
                        {
                            "review": review.model_dump(mode="json"),
                            "previous_output_sha256": hashlib.sha256(
                                previous_output.encode()
                            ).hexdigest(),
                        },
                    )

            def reviewed_plan(*args, **kwargs):
                return application_plan(*args, initial_correction=semantic_correction, **kwargs)

            with (
                patch.object(stories, "planner", budgeted),
                patch.object(stories, "plan_story", reviewed_plan),
                patch.dict(
                    os.environ,
                    {
                        "KOMAQUILL_STORY_PURPOSE": "verification",
                        "KOMAQUILL_STORY_CASE_ID": case_id,
                    },
                ),
            ):
                try:
                    result = Client(workspace.root).stories.plan(
                        case["source"],
                        source_name=case_id + ".txt",
                        images=4,
                        llm_profile=llm_profile,
                        model="klein-4b",
                        allow_real_llm=True,
                    )
                    story_id = result.request.story_id
                except KomaquillError as error:
                    failure = error
                    story_id = (
                        UUID(error.details["story_id"]) if "story_id" in error.details else None
                    )
                except PlanningBudgetExhausted:
                    failure = KomaquillError(
                        code="operation_failed",
                        stage="story_planning",
                        details={"reason": "planning_budget_exhausted"},
                    )
            measured = planning_measurement(
                Client(workspace.root),
                story_id,
                case_id=case_id,
                count=4,
                source_sha256=hashlib.sha256(case["source"].encode()).hexdigest(),
                started_at=started_at,
                elapsed_seconds=time.monotonic() - before,
                failure=failure,
            )
            atomic_json(folder / "measurement.json", measured)
            with FileLock(str(report / ".suite.lock")):
                latest = load(report)
                latest.plans.setdefault(case_id, []).append(
                    {
                        "story_id": str(story_id) if story_id else None,
                        "status": measured["status"],
                        "llm_profile": llm_profile,
                        "semantic_repair_of": semantic_parent,
                        "measurement": str((folder / "measurement.json").relative_to(report)),
                    }
                )
                save(report, latest)
        finally:
            try:
                atomic_bytes(config_path, original_config)
            finally:
                restored = llm_comparison.restore(executable, original, allowed, folder)
                atomic_json(
                    folder / "restored.json",
                    {"models": restored, "matches_original": restored == original},
                )


def prepare(report: Path) -> None:
    """Stage only attributable, semantically accepted real-model plans."""
    suite = load(report)
    if suite.members or suite.gpu_started_at is not None:
        raise RuntimeError("This suite has already been prepared.")
    reviews = [
        PlanReview.model_validate(row)
        for row in json.loads((report / "planning-reviews.json").read_bytes())
    ]
    repository = FileStoryRepository(Project(report / "planning"))
    from komaquill.infrastructure.artifacts import file_digest
    from komaquill.infrastructure.asset_store import model_path

    source = Project(suite.source_workspace)
    for model in assets(source.settings()):
        if file_digest(model_path(source.models, model)) != model.sha256:
            raise ValueError("A pinned source model has changed.")
    for case_id in suite.config.live_cases:
        accepted = [row for row in reviews if row.case_id == case_id and row.decision == "accept"]
        if not accepted:
            continue
        row = accepted[-1]
        plan = repository.read(row.story_id)
        if (
            row.evaluator.kind != "ai"
            or plan.request.source_sha256 != row.source_sha256
            or plan.request.source_text != cases()[case_id].source
            or plan.planner.provider == "fake"
            or not any(
                item["story_id"] == str(row.story_id) and item["status"] == "saved"
                for item in suite.plans.get(case_id, [])
            )
            or json.loads((repository.folder(row.story_id) / "plan.json").read_bytes())["sha256"]
            != row.plan_sha256
        ):
            raise ValueError("The plan review does not match retained real-model evidence.")
        story_completion.prepare(
            report / "planning",
            row.story_id,
            report / "cases" / case_id,
            workspace=report / "workspace",
            config=suite.config,
            model_workspace=suite.source_workspace,
        )
        suite.members.append(case_id)
        save(report, suite)
    if not suite.members:
        raise RuntimeError("No real-model plan passed semantic review; no GPU was allocated.")


def start(report: Path) -> None:
    with FileLock(str(report / ".suite.lock")):
        suite = load(report)
        if not suite.members:
            raise RuntimeError("Prepare the suite before GPU startup.")
        if suite.gpu_started_at is not None and not suite.shutdown_confirmed:
            raise RuntimeError("Recover or confirm shutdown of the owned suite runtime first.")
        suite.shutdown_confirmed = suite.stopping = False
        settings = story_completion.project(report / "cases" / suite.members[0]).settings()
        if (
            settings.submission_timeout != suite.config.send_seconds
            or settings.session_timeout != suite.config.total_seconds
        ):
            raise ValueError("Effective GPU deadlines differ from the frozen suite limits.")
        suite.gpu_started_at = datetime.now(UTC)
        save(report, suite)
    first = report / "cases" / suite.members[0]
    try:
        story_completion.start(first)
    finally:
        runtime = story_completion.load(first).runtime_id
        for case_id in suite.members:
            folder = report / "cases" / case_id
            member = story_completion.load(folder)
            member.gpu_started_at, member.runtime_id = suite.gpu_started_at, runtime
            story_completion.save(folder, member)


def reserve_operation(report: Path, key: str, count: int) -> bool:
    """Keep uncertain reservations charged across interruption and process restart."""
    with FileLock(str(report / ".suite.lock")):
        return _reserve_operation(report, key, count)


def _reserve_operation(report: Path, key: str, count: int) -> bool:
    suite = load(report)
    if count < 1:
        raise ValueError("Output reservations must be positive.")
    if suite.stopping or suite.shutdown_confirmed:
        raise RuntimeError("The suite is stopping; new requests are disabled.")
    if key in suite.operations:
        return False
    elapsed = (
        (datetime.now(UTC) - suite.gpu_started_at).total_seconds()
        if suite.gpu_started_at
        else float("inf")
    )
    if (
        elapsed >= suite.config.send_seconds
        or sum(suite.operations.values()) + count > suite.config.output_limit
    ):
        raise RuntimeError("The suite submission budget is exhausted.")
    models = report / "workspace/.artifacts/data/models"
    size = sum(
        path.stat().st_size
        for path in report.rglob("*")
        if path.is_file() and not path.is_relative_to(models)
    )
    if size >= suite.config.storage_limit:
        raise RuntimeError("The suite storage budget is exhausted.")
    suite.operations[key] = count
    save(report, suite)
    return True


def baseline(report: Path) -> None:
    with FileLock(str(report / ".operation.lock"), timeout=0):
        suite = load(report)
        for case_id in suite.members:
            folder = report / "cases" / case_id
            member = story_completion.load(folder)
            fresh = reserve_operation(
                report, "baseline-" + case_id, member.scene_count * suite.config.candidates
            )
            if not fresh and not member.baseline_reserved:
                raise RuntimeError("A baseline reservation is unresolved; do not resubmit.")
            try:
                story_completion.baseline(folder)
            except Exception:
                view = Client(story_completion.project(folder).root).stories.show(member.story_id)
                if not view.finished:
                    raise


def improve(report: Path, case_id: str, request: Path) -> None:
    with FileLock(str(report / ".operation.lock"), timeout=0):
        suite = load(report)
        if case_id not in suite.members:
            raise ValueError("The case does not belong to this suite.")
        from komaquill.domain.workflow import digest

        data = story_completion.ImprovementRequest.model_validate_json(request.read_bytes())
        request_hash = digest(data.model_dump(mode="json"))
        prefix = "improvement-"
        key = prefix + case_id + "-" + request_hash
        if (
            key not in suite.operations
            and sum(name.startswith(prefix) for name in suite.operations)
            >= suite.config.improvement_limit
        ):
            raise RuntimeError("The suite improvement budget is exhausted.")
        fresh = reserve_operation(report, key, suite.config.candidates)
        folder = report / "cases" / case_id
        known = any(
            json.loads(job.source.content)["request_sha256"] == request_hash
            for job in story_completion.load(folder).improvements
        )
        if not fresh and not known:
            raise RuntimeError("An improvement reservation is unresolved; do not resubmit.")
        story_completion.improve(folder, request)


def retry(report: Path, case_id: str) -> None:
    with FileLock(str(report / ".operation.lock"), timeout=0):
        suite = load(report)
        if case_id not in suite.members:
            raise ValueError("The case does not belong to this suite.")
        folder = report / "cases" / case_id
        member = story_completion.load(folder)
        view = Client(story_completion.project(folder).root).stories.show(member.story_id)
        outputs = (
            sum(scene.state in {"failed", "lost"} for scene in view.scenes)
            * suite.config.candidates
        )
        used = sum(count for name, count in suite.operations.items() if name.startswith("retry-"))
        if not outputs or used + outputs > suite.config.technical_retry_limit:
            raise RuntimeError("The confirmed failure retry budget is exhausted.")
        if not reserve_operation(report, "retry-" + case_id, outputs):
            raise RuntimeError("A retry was already reserved; reconcile its retained runs.")
        story_completion.baseline(folder, retry_failed=True)


def stop(report: Path) -> None:
    with FileLock(str(report / ".suite.lock")):
        suite = load(report)
        suite.stopping = True
        save(report, suite)
    if not suite.members:
        return
    first = report / "cases" / suite.members[0]
    story_completion.stop(first)
    path = first / "shutdown.json"
    if path.is_file():
        receipt = json.loads(path.read_bytes())
        atomic_json(report / "shutdown.json", receipt)
        with FileLock(str(report / ".suite.lock")):
            latest = load(report)
            latest.shutdown_confirmed = receipt.get("gpu") == "stopped" and not receipt.get(
                "errors"
            )
            save(report, latest)


def summarize(report: Path) -> None:
    suite = load(report)
    rows = []
    for case_id in suite.config.live_cases:
        folder = report / "cases" / case_id
        if case_id not in suite.members:
            rows.append(
                {
                    "case_id": case_id,
                    "generated": False,
                    "quality_passed": None,
                    "generation_status": "not_submitted",
                    "quality_status": "not_generated",
                }
            )
            continue
        completion_report.publish(folder)
        manifest = json.loads((folder / "manifest.json").read_bytes())
        original = folder / "baseline-manifest.json"
        manifest["initial_quality_passed"] = (
            json.loads(original.read_bytes())["quality_passed"] if original.is_file() else False
        )
        rows.append(
            {
                **manifest,
                "case_id": case_id,
                "completion_case_id": manifest["case_id"],
            }
        )
    atomic_json(
        report / "summary.json",
        {
            "format_version": 1,
            "identity": suite.identity,
            "planning_calls": suite.planning_calls,
            "reserved_outputs": sum(suite.operations.values()),
            "expected_stories": len(suite.config.live_cases),
            "stories": rows,
            "quality_passed_stories": sum(bool(row.get("quality_passed")) for row in rows),
            "initial_quality_passed_stories": sum(
                bool(row.get("initial_quality_passed")) for row in rows
            ),
            "comic_complete_stories": sum(bool(row.get("comic_complete")) for row in rows),
            "shutdown_confirmed": suite.shutdown_confirmed,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "initialize",
            "plan",
            "prepare",
            "start",
            "baseline",
            "improve",
            "retry",
            "summarize",
            "stop",
        ),
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--case")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--lms", type=Path, default=Path.home() / ".lmstudio/bin/lms.exe")
    parser.add_argument(
        "--llm-profile", choices=("lmstudio-4b", "lmstudio-9b"), default="lmstudio-4b"
    )
    parser.add_argument("--allow-real-llm", action="store_true")
    parser.add_argument("--allow-gpu", action="store_true")
    args = parser.parse_args()
    if args.action == "plan" and (not args.allow_real_llm or not args.case):
        parser.error("Planning requires --allow-real-llm and --case.")
    if args.action in {"start", "baseline", "improve", "retry", "stop"} and not args.allow_gpu:
        parser.error("This live operation requires --allow-gpu.")
    if args.action == "initialize":
        initialize(args.source, args.report)
    elif args.action == "plan":
        plan(args.report, args.case, args.lms, llm_profile=args.llm_profile)
    elif args.action == "improve":
        if not args.case or not args.request:
            parser.error("Improvement requires --case and --request.")
        improve(args.report, args.case, args.request)
    elif args.action == "retry":
        if not args.case:
            parser.error("Retry requires --case.")
        retry(args.report, args.case)
    else:
        globals()[args.action](args.report)
