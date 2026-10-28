"""Freeze diverse image comparisons and retain uncertain request reservations."""

import json
from collections import Counter

import pytest

from komaquill.api import Client, Evaluator
from komaquill.application.generation import collect, generate
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.configuration import Project
from tests.live import klein_comparison as comparison
from tests.live.completion_report import REQUIRED_CHECKS


@pytest.fixture
def prepared(project, session, client, tmp_path):
    active = session.model_copy(update={"settings": project.settings()})
    parents = []
    for scene in (3, 4):
        run = generate(project, client, active, "a red book beside a blue bag", 42 + scene)
        name = run.images[0]
        parents.append(
            {
                "scene": scene,
                "key": f"runs/{run.run_id}/{name}",
                "sha256": run.image_metadata[name].sha256,
                "original": "Preserve the room and fix the bag.",
                "visible_state": "One closed opaque blue bag beside the same table.",
            }
        )
    file = tmp_path / "parents.json"
    file.write_text(json.dumps(parents), encoding="utf-8")
    report = tmp_path / "comparison"
    comparison.prepare(report, project.root, file)
    return report


def test_comparison_pairs_conditions_and_parents_without_claiming_llm_success(prepared):
    frozen, config, jobs = comparison.load(prepared)
    assert len(jobs) == config.output_limit - config.technical_retry_limit
    assert Counter(job.group for job in jobs) == {
        "baseline": 32,
        "edit": 2,
        "ablation": 1,
    }
    assert len({job.id for job in jobs}) == len(jobs)
    for job in [job for job in jobs if job.group == "baseline" and job.profile == "klein-4b"]:
        other = next(
            row
            for row in jobs
            if row.group == job.group
            and row.case == job.case
            and row.scene == job.scene
            and row.seed == job.seed
            and row.profile == "wai-illustrious"
        )
        assert (job.prompt, job.subjects, job.parameters) == (
            other.prompt,
            other.subjects,
            other.parameters,
        )
    edits = [job for job in jobs if job.group == "edit"]
    assert len({job.parent_sha256 for job in edits}) <= 2
    assert all(plan["planner"]["provider"] == "curated" for plan in frozen["plans"].values())
    assert len(frozen["plans"]["rainy-cat"]["scenes"][3]["subjects"]) == 3
    assert not frozen["plans"]["flying-hat"]["characters"]
    for job in jobs:
        assert json.loads(job.source.content)["negative"] is None
    assert Counter(row["kind"] for row in comparison.expected_pairs(jobs)) == {
        "model": 16,
        "lora": 1,
        "edit_model": 1,
    }


def test_comparison_does_not_resubmit_after_uncertain_failure(prepared, monkeypatch):
    from komaquill.api.services import ImageService

    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise KomaquillError(code="submission_unknown")

    monkeypatch.setattr(ImageService, "generate", fail)
    with pytest.raises(KomaquillError):
        comparison.generate(prepared, "baseline")
    with pytest.raises(ValueError, match="unknown acceptance"):
        comparison.generate(prepared, "baseline")
    assert len(calls) == 1
    state = json.loads((prepared / "state.json").read_bytes())
    assert len(state["jobs"]) == 1


def test_comparison_summarizes_missing_images_without_placeholder_comics(prepared):
    comparison.summarize(prepared)
    summary = json.loads((prepared / "summary.json").read_bytes())
    assert summary["saved_images"] == 0 and summary["unrun_images"] == 35
    assert summary["quality_approved_images"] is None
    assert not summary["comparison_complete"]
    assert not (prepared / "comics").exists()


def test_frozen_comparison_is_never_overwritten(prepared):
    before = (prepared / "comparison.json").read_bytes()
    with pytest.raises(ValueError):
        comparison.prepare(prepared, prepared, prepared / "missing.json")
    assert (prepared / "comparison.json").read_bytes() == before


def test_changed_frozen_conditions_are_rejected(prepared):
    path = prepared / "comparison.json"
    data = json.loads(path.read_bytes())
    data["config"]["seeds"][0] += 1
    atomic_json(path, data)
    with pytest.raises(ValueError, match="changed"):
        comparison.load(prepared)


def test_reader_corrections_do_not_unlock_new_execution(prepared, monkeypatch):
    original = Project.identity
    monkeypatch.setattr(
        Project, "identity", lambda self: {**original(self), "source_revision": "changed"}
    )
    comparison.load(prepared)
    with pytest.raises(ValueError, match="executable code changed"):
        comparison.load(prepared, executing=True)


@pytest.mark.parametrize("view", ["app", "graph"])
def test_interface_candidate_is_adopted_only_with_its_executed_conditions(
    prepared,
    client,
    session,
    view,
):
    project = Project(prepared / "workspace")
    api = Client(project.root)
    frozen, _, jobs = comparison.load(prepared)
    job = next(row for row in jobs if row.group == "baseline" and row.profile == "klein-4b")
    exported = comparison.export_ui(prepared, job.id, view)
    graph, _ = comparison.job_workflow(project, job)
    client.submit(graph, json.loads(exported.read_bytes()), "browser-test")
    collect(project, client, session.model_copy(update={"settings": project.settings()}))
    comparison.adopt_ui(prepared, job.id)
    state = json.loads((prepared / "state.json").read_bytes())
    run, name, _, _ = comparison.saved_candidate(project, api, job, state["jobs"][job.id])
    comparison.summarize(prepared)
    summary = json.loads((prepared / "summary.json").read_bytes())
    assert summary["valid_saved_images"] == 1
    assert summary["quality_approved_images"] is None
    assert summary["images"][0]["queue_seconds"] is None
    assert summary["images"][0]["queue_observation"] == "first_observation_only"
    assert not summary["comparison_complete"]
    review = comparison.Review(
        scene=job.scene,
        run_id=run.run_id,
        image=name,
        decision="undecided",
        reason="Synthetic test pixels cannot demonstrate scene quality.",
        checks=[
            {"criterion": key, "verdict": "unclear", "reason": "Synthetic test image."}
            for key in REQUIRED_CHECKS
        ]
        + [
            {"criterion": key, "verdict": "unclear", "reason": "Synthetic test image."}
            for key in frozen["conditions"][job.case]["scenes"][str(job.scene)]["conditions"]
        ],
    )
    reviewed = comparison.ReviewFile(
        evaluator=Evaluator(
            kind="ai", name="test-fixture", model="fake", instruction="Test attribution only."
        ),
        reviews=[review],
    )
    review_file = prepared / "review-test.json"
    atomic_json(review_file, reviewed.model_dump(mode="json"))
    comparison.record_reviews(prepared, review_file)
    comparison.record_reviews(prepared, review_file)
    comparison.summarize(prepared)
    summary = json.loads((prepared / "summary.json").read_bytes())
    assert summary["quality_reviewed_images"] == 1
    assert summary["quality_approved_images"] == 0
    condition_ids = set(frozen["conditions"][job.case]["scenes"][str(job.scene)]["conditions"])
    assert condition_ids <= {check["id"] for check in summary["images"][0]["checks"]}
    assert len(api.evaluations.list()) == 1
    changed = job.model_copy(update={"seed": job.seed + 1})
    assert not comparison.ui_matches(project, api, changed, run)
    api.artifact_path(f"runs/{run.run_id}/{name}").unlink()
    with pytest.raises((ValueError, KomaquillError)):
        comparison.saved_candidate(project, api, job, state["jobs"][job.id])


def test_local_approval_cannot_be_counted_as_scene_quality(prepared):
    frozen, _, jobs = comparison.load(prepared)
    job = next(job for job in jobs if job.group == "baseline")
    checks = [
        {"criterion": key, "verdict": "match", "reason": "Synthetic contract fixture."}
        for key in REQUIRED_CHECKS
    ]
    for key in frozen["conditions"][job.case]["scenes"][str(job.scene)]["conditions"]:
        checks.append(
            {"criterion": key, "verdict": "match", "reason": "Synthetic contract fixture."}
        )
    from uuid import uuid4

    local = comparison.Review(
        scene=job.scene,
        run_id=uuid4(),
        image="test.png",
        scope="local",
        decision="accept",
        reason="Only the edited region was checked.",
        checks=checks,
    )
    assert not comparison.scene_approved(frozen, job, local)
    assert comparison.scene_approved(frozen, job, local.model_copy(update={"scope": "scene"}))
    assert not comparison.scene_approved(
        frozen,
        job,
        local.model_copy(update={"scope": "scene", "checks": local.checks[: len(REQUIRED_CHECKS)]}),
    )


def test_recovery_target_preserves_conditions_and_requires_cumulative_budget(prepared, monkeypatch):
    frozen, _, _ = comparison.load(prepared)
    original = (prepared / "comparison.json").read_bytes()
    state = json.loads((prepared / "state.json").read_bytes())
    state["sessions"]["T4-1"] = {
        "status": "stopped",
        "runtime_id": "original",
        "ownership_released": True,
    }
    atomic_json(prepared / "state.json", state)
    with pytest.raises(ValueError, match="cumulative verification budget"):
        comparison.start(prepared)
    assert (prepared / "comparison.json").read_bytes() == original
    wrong = {
        "format_version": 1,
        "comparison_sha256": "0" * 64,
        "identity": frozen["identity"],
        "runner_sha256": frozen["runner_sha256"],
        "session_limit": 2,
        "created_at": "2026-10-27T00:00:00Z",
        "reason": "Synthetic authorization fixture.",
    }
    (prepared / "execution-revisions.jsonl").write_text(json.dumps(wrong) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="another comparison"):
        comparison.load(prepared, executing=True)
    assert (
        json.loads((prepared / "state.json").read_bytes())["sessions"]["T4-1"]["status"]
        == "stopped"
    )


def test_each_case_criteria_identify_its_own_scene(prepared):
    frozen, _, _ = comparison.load(prepared)
    conditions = comparison.review_conditions(prepared, frozen)
    for key, value in frozen["plans"].items():
        plan = comparison.StoryPlan.model_validate(value)
        for scene in plan.scenes:
            actual = conditions[key]["scenes"][str(scene.position)]
            assert actual == comparison.scene_conditions(key, scene)
    assert "rainy-cat:scene:1:subject:woman" in conditions["rainy-cat"]["scenes"]["1"]["conditions"]
    assert not any(
        "subject:" in key for key in conditions["flying-hat"]["scenes"]["1"]["conditions"]
    )


def test_explicit_criteria_correction_preserves_frozen_plan_and_source(prepared):
    frozen, _, _ = comparison.load(prepared)
    original = (prepared / "comparison.json").read_bytes()
    correction = {
        "format_version": 1,
        "comparison_sha256": comparison.digest(frozen),
        "original_conditions_sha256": comparison.digest(frozen["conditions"]),
        "reason": "Synthetic correction fixture.",
        "conditions": frozen["conditions"],
    }
    atomic_json(prepared / "condition-correction.json", correction)
    assert comparison.review_conditions(prepared, frozen) == frozen["conditions"]
    correction["conditions"]["rainy-cat"]["scenes"] = correction["conditions"]["group-photo"][
        "scenes"
    ]
    atomic_json(prepared / "condition-correction.json", correction)
    with pytest.raises(ValueError, match="frozen source and scene"):
        comparison.review_conditions(prepared, json.loads(original))
    assert (prepared / "comparison.json").read_bytes() == original


def test_real_plan_validation_freezes_only_required_model_seed_combinations(project, monkeypatch):
    from tests.live import story_validation
    from tests.support.story_cases import cases

    config = comparison.configuration()
    from uuid import uuid4

    from komaquill.domain.stories import Scene
    from tests.support.stories import request

    base = comparison.fixed_plan("rainy-cat", config)
    plans = {}
    for key, case in cases().items():
        data = case.draft.model_dump(exclude={"scenes"})
        data.update(
            request=request(4, case.source),
            scenes=[
                Scene(
                    **scene.model_dump(),
                    scene_id=uuid4(),
                    position=index,
                    characters=[subject.character for subject in scene.subjects],
                )
                for index, scene in enumerate(case.draft.scenes, 1)
            ],
        )
        plans[key] = comparison.StoryPlan.model_validate({**base.model_dump(), **data})
    monkeypatch.setattr(story_validation, "reviewed_plans", lambda _: plans)
    report = project.root / "validation"
    story_validation.freeze(report, project.root / "mock-review")
    frozen, _, jobs = comparison.load(report, executing=True)
    assert len(jobs) == 64
    assert Counter(job.profile for job in jobs) == {"klein-4b": 48, "wai-illustrious": 16}
    assert len(frozen["plans"]) == 8
    assert len([job for job in jobs if job.case == "red-book" and job.profile == "klein-4b"]) == 8
    assert len([job for job in jobs if job.case == "morning"]) == 4
    comparison.summarize(report)
    summary = json.loads((report / "summary.json").read_bytes())
    assert len(summary["works"]) == 16
    assert all(row["quality_approved"] is None for row in summary["works"])
    assert all(row["generation_status"] == "not_submitted" for row in summary["works"])


def test_reallocations_use_cumulative_budget_and_do_not_require_session_count_approval(
    prepared, monkeypatch
):
    from datetime import UTC, datetime
    from uuid import uuid4

    from komaquill.api.services import SessionService
    from komaquill.infrastructure.colab import Colab, Receipt
    from tests.live import verification_budget as budget

    project = Project(prepared / "workspace")
    parent = {
        "saved_images": 17,
        "compute_budget": {
            "initial_balance": 10,
            "latest": {"remaining_compute_units": 9, "account_sha256": "a" * 64},
        },
    }
    budget.initialize(
        project.root,
        parent,
        dict(
            compute_unit_limit=9,
            compute_unit_reserve=0.5,
            output_limit=100,
            gpu_seconds=16200,
            session_seconds=5400,
            submission_seconds=4200,
        ),
    )
    from types import SimpleNamespace

    budget.reconcile_opening_usage(
        project.root,
        parent,
        SimpleNamespace(assignments_count=0, account_sha256="a" * 64, remaining_compute_units=9),
    )

    def start(service, **kwargs):
        settings = project.settings()
        receipt = Receipt(
            name="test", runtime_id=uuid4(), settings=settings, started_at=datetime.now(UTC)
        )
        atomic_json(Colab(project).receipt_file, receipt.model_dump(mode="json"))

    def stop(service, **kwargs):
        control = Colab(project)
        receipt = control.receipt()
        atomic_json(
            project.logs / str(receipt.runtime_id) / "shutdown.json",
            {"gpu": "stopped", "finished_at": datetime.now(UTC).isoformat()},
        )
        control.receipt_file.unlink()

    monkeypatch.setattr(SessionService, "start", start)
    monkeypatch.setattr(SessionService, "stop", stop)
    for _ in range(5):
        comparison.start(prepared)
        comparison.stop(prepared)
    state = json.loads((prepared / "state.json").read_bytes())
    assert len(state["sessions"]) == 5
    assert all(row.ownership_released for row in budget.read(project.root).allocations)
    assert budget.read(project.root).carried_images == 17
