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
        "baseline": 64,
        "edit": 8,
        "ablation": 4,
        "regression": 2,
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
            and row.profile == "klein-9b"
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
        assert "negative" not in json.loads(job.source.content)
    assert Counter(row["kind"] for row in comparison.expected_pairs(jobs)) == {
        "model": 32,
        "lora": 4,
        "edit_model": 4,
        "edit_instruction": 4,
    }


def test_comparison_does_not_resubmit_after_uncertain_failure(prepared, monkeypatch):
    from komaquill.api.services import ImageService

    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise KomaquillError(code="submission_unknown")

    monkeypatch.setattr(ImageService, "generate", fail)
    with pytest.raises(KomaquillError):
        comparison.generate(prepared, "regression")
    with pytest.raises(ValueError, match="unknown acceptance"):
        comparison.generate(prepared, "regression")
    assert len(calls) == 1
    state = json.loads((prepared / "state.json").read_bytes())
    assert len(state["jobs"]) == 1


def test_comparison_summarizes_missing_images_without_placeholder_comics(prepared):
    comparison.summarize(prepared)
    summary = json.loads((prepared / "summary.json").read_bytes())
    assert summary["saved_images"] == 0 and summary["unrun_images"] == 78
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
    _, _, jobs = comparison.load(prepared)
    job = next(row for row in jobs if row.id == "regression-generate")
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
    assert len(api.evaluations.list()) == 1
    changed = job.model_copy(update={"seed": job.seed + 1})
    assert not comparison.ui_matches(project, api, changed, run)
    api.artifact_path(f"runs/{run.run_id}/{name}").unlink()
    with pytest.raises((ValueError, KomaquillError)):
        comparison.saved_candidate(project, api, job, state["jobs"][job.id])
