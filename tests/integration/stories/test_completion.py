"""Verify completion controls and evidence using FakeListLLM and the ComfyUI stub."""

import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from komaquill.api import Client, services, stories
from tests.live import completion_report as publication
from tests.live import story_completion as live
from tests.support.stories import planned


def test_shutdown_summary_uses_the_current_recorded_runtime(completion, monkeypatch):
    from komaquill.infrastructure.atomic import atomic_json

    report, _, _ = completion
    case = live.load(report)
    original, current = uuid4(), uuid4()
    case.runtime_id = current
    live.save(report, case)
    for identity in [current, original]:
        atomic_json(
            live.project(report).logs / str(identity) / "shutdown.json",
            {"runtime_id": str(identity), "gpu": "stopped"},
        )
    monkeypatch.setattr(services.SessionService, "stop", lambda *args, **kwargs: None)
    live.stop(report)
    assert json.loads((report / "shutdown.json").read_bytes())["runtime_id"] == str(current)


def test_multiple_cases_share_one_workspace_and_model_set(completion, project):
    report, _, _ = completion
    _, another = planned(project, count=2)
    second = report.parent / "second-case"
    live.prepare(
        project.root,
        another.request.story_id,
        second,
        workspace=live.project(report).root,
    )
    assert live.project(report).root == live.project(second).root
    assert live.load(second).scene_count == 2
    assert live.load(report).scene_count == 4
    assert len(list(live.project(report).models.rglob("*.safetensors"))) == 5
    assert live.load(report).source_plan_sha256 != live.load(second).source_plan_sha256


def test_model_definitions_follow_the_shared_model_workspace(completion, project):
    from komaquill.infrastructure.configuration import Project

    report, _, _ = completion
    planning_root = project.root / "separate-planning"
    Client.initialize(planning_root)
    _, plan = planned(Project(planning_root))
    destination = report.parent / "separate-case"
    live.prepare(planning_root, plan.request.story_id, destination, model_workspace=project.root)
    assert live.project(destination).settings().assets == project.settings().assets


def test_preparation_checkpoints_runtime_before_start_returns(completion, monkeypatch):
    from komaquill.infrastructure.atomic import atomic_json

    report, _, _ = completion
    case = live.load(report)
    case.gpu_started_at = None
    live.save(report, case)
    runtime_id = uuid4()

    def starting(self, *, progress, **kwargs):
        atomic_json(self._project.state / "monitor.json", {"runtime_id": str(runtime_id)})
        progress("Preparing the verified model.")
        assert live.load(report).runtime_id == runtime_id
        raise OSError("Injected preparation interruption")

    monkeypatch.setattr(services.SessionService, "start", starting)
    with pytest.raises(OSError):
        live.start(report)
    assert live.load(report).runtime_id == runtime_id
    atomic_json(live.project(report).state / "monitor.json", {"runtime_id": str(uuid4())})
    with pytest.raises(RuntimeError):
        live.checkpoint_runtime(report)
    assert live.load(report).runtime_id == runtime_id


@pytest.fixture
def completion(project, session, client, monkeypatch):
    repository, plan = planned(project)
    from tests.support.models import stage_models

    stage_models(project)
    report = project.root / "completion"
    case = live.prepare(
        project.root, plan.request.story_id, report, workspace=project.root / "shared-workspace"
    )
    case.gpu_started_at = datetime.now(UTC)
    live.save(report, case)
    active = session.model_copy(update={"settings": live.project(report).settings()})

    @contextmanager
    def connected(_):
        yield client, active

    monkeypatch.setattr(services, "connected", connected)
    monkeypatch.setattr(stories, "connected", connected)
    return report, repository, plan


def reviews_for(report):
    case = live.load(report)
    api = Client(live.project(report).root)
    snapshot = api.stories.show(case.story_id)
    reviews, selections = [], []
    for scene in snapshot.scenes:
        for image in scene.candidates:
            reviews.append(
                {
                    "scene": scene.position,
                    "run_id": str(scene.run_id),
                    "image": image.key.split(f"runs/{scene.run_id}/", 1)[1],
                    "decision": "reject",
                    "reason": "The subject does not match the scene.",
                    "issues": ["species"],
                    "checks": [
                        {"criterion": key, "verdict": "unclear", "reason": "Offline fixture"}
                        for key in publication.REQUIRED_CHECKS
                    ],
                }
            )
        selections.append(
            {
                "scene": scene.position,
                "key": scene.candidates[0].key,
                "reason": "Best provisional candidate; quality remains rejected.",
            }
        )
    (report / "reviews.json").write_text(
        json.dumps(
            {
                "format_version": 2,
                "reviews": reviews,
                "selections": selections,
                "evaluator": {
                    "kind": "ai",
                    "name": "Offline test evaluator",
                    "model": "fake-vision",
                    "instruction": publication.INSTRUCTION,
                },
            }
        )
    )
    return snapshot


def test_all_scenes_finish_despite_rejected_quality_and_reports_preserve_originals(
    completion, stub
):
    report, source_repository, plan = completion
    origin = source_repository.folder(plan.request.story_id)
    original = (origin / "plan.json").read_bytes()
    live.baseline(report)
    assert stub.submissions == 4
    snapshot = reviews_for(report)
    assert snapshot.complete and len(snapshot.scenes) == 4
    assert all(len(scene.candidates) == 1 for scene in snapshot.scenes)
    publication.publish(report)
    manifest = json.loads((report / "manifest.json").read_bytes())
    assert manifest["baseline_complete"] and manifest["selection_complete"]
    assert manifest["selection_is_quality_approval"] is False
    assert manifest["quality_passed"] is False
    assert manifest["selected_quality"]["reject"] == 4
    assert {row["decision"] for row in manifest["reviews"]} == {"reject"}
    assert original == (origin / "plan.json").read_bytes()
    assert (
        original
        == (
            live.FileStoryRepository(live.project(report)).folder(plan.request.story_id)
            / "plan.json"
        ).read_bytes()
    )
    before = (report / "baseline-selection.md").read_bytes()
    publication.publish(report)
    assert (report / "baseline-selection.md").read_bytes() == before
    assert len(Client(live.project(report).root).evaluations.list()) == 4
    live.baseline(report)
    assert stub.submissions == 4
    assert live.load(report).reserved_outputs == 4


def test_improvement_is_a_separate_run_with_frozen_parent_and_only_one_submission(completion, stub):
    report, _, _ = completion
    live.baseline(report)
    snapshot = reviews_for(report)
    publication.publish(report)
    original_selection = (report / "baseline-selection.md").read_bytes()
    request = report / "improve.json"
    request.write_text(
        json.dumps(
            {
                "scene": 3,
                "method": "regenerate",
                "parent_key": snapshot.scenes[2].candidates[0].key,
                "prompt": "two gray cats under the awning",
                "subjects": ["one adult cat", "one kitten"],
            }
        )
    )
    live.improve(report, request)
    first = live.load(report)
    assert first.reserved_outputs == 5
    assert first.improvements[0].run_id not in snapshot.scenes[2].attempt_ids
    assert stub.submissions == 5
    live.improve(report, request)
    assert stub.submissions == 5 and live.load(report).reserved_outputs == 5
    publication.publish(report)
    manifest = json.loads((report / "manifest.json").read_bytes())
    assert len(manifest["images"]) == 5
    assert len(manifest["selections"]) == 4
    assert (report / "baseline-selection.md").read_bytes() == original_selection
    assert (report / "before-after.md").is_file()


def test_relative_report_recovers_baseline_after_an_unselected_edit(completion, monkeypatch):
    report, _, _ = completion
    live.baseline(report)
    snapshot = reviews_for(report)
    request = report / "edit.json"
    request.write_text(
        json.dumps(
            {
                "scene": 1,
                "method": "edit",
                "parent_key": snapshot.scenes[0].candidates[0].key,
                "prompt": "Preserve the source action and correct the visible prop connection.",
            }
        )
    )
    live.improve(report, request)
    assert not (report / "baseline-manifest.json").exists()
    monkeypatch.chdir(report.parent)
    relative = Path(report.name)
    publication.publish(relative)
    initial = json.loads((report / "baseline-manifest.json").read_bytes())
    current = json.loads((report / "manifest.json").read_bytes())
    assert len(initial["images"]) == 4 and not initial["improvements"]
    assert len(current["images"]) == 5 and len(current["improvements"]) == 1
    assert initial["selected_quality"] == current["selected_quality"]
    assert initial["comic_complete"]
    for name in ["all-candidates.md", "selected.md", "baseline-selection.md", "before-after.md"]:
        links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", (report / name).read_text())
        assert links and all((report / link).is_file() for link in links)


@pytest.mark.parametrize("missing_review", [False, True])
def test_quality_pass_requires_an_accept_review_for_every_selected_image(
    completion, missing_review
):
    report, _, _ = completion
    live.baseline(report)
    reviews_for(report)
    path = report / "reviews.json"
    payload = json.loads(path.read_bytes())
    for row in payload["reviews"]:
        row["decision"] = "accept"
        for check in row["checks"]:
            check["verdict"] = "match"
    if missing_review:
        payload["reviews"].pop()
    path.write_text(json.dumps(payload))
    publication.publish(report)
    manifest = json.loads((report / "manifest.json").read_bytes())
    assert manifest["selection_complete"]
    assert manifest["quality_passed"] is (not missing_review)
    assert manifest["selected_quality"] == {
        "accept": 3 if missing_review else 4,
        "reject": 0,
        "undecided": 0,
        "unreviewed": int(missing_review),
    }


def test_budget_and_deadline_are_not_reset_on_resume(completion):
    report, _, _ = completion
    case = live.load(report)
    live.reserve(case, case.config.output_limit)
    live.save(report, case)
    with pytest.raises(RuntimeError):
        live.reserve(live.load(report), 1)
    case.gpu_started_at = datetime.now(UTC) - timedelta(seconds=case.config.send_seconds)
    live.save(report, case)
    with pytest.raises(RuntimeError):
        live.sending_allowed(live.load(report))
    with pytest.raises(RuntimeError):
        live.start(report)


def test_unresolved_baseline_is_never_automatically_resubmitted(completion, stub):
    report, _, _ = completion
    case = live.load(report)
    case.baseline_reserved = True
    case.reserved_outputs = 4
    live.save(report, case)
    with pytest.raises(RuntimeError):
        live.baseline(report)
    assert stub.submissions == 0


def test_frozen_config_error_does_not_block_owned_gpu_shutdown(completion, monkeypatch):
    report, _, _ = completion
    (live.project(report).root / "komaquill.toml").write_text("corrupt")
    with pytest.raises(RuntimeError):
        live.load(report)
    stopped = []
    monkeypatch.setattr(services.SessionService, "stop", lambda self, **kw: stopped.append(kw))
    live.stop(report)
    assert len(stopped) == 1
    (report / "case.json").write_text("{}")
    live.stop(report)
    assert stopped[-1]["force"]


def test_unknown_improvement_response_is_reconciled_without_resending(
    completion, monkeypatch, stub
):
    report, _, _ = completion
    live.baseline(report)
    snapshot = reviews_for(report)
    request = report / "improve.json"
    request.write_text(
        json.dumps(
            {
                "scene": 1,
                "method": "regenerate",
                "parent_key": snapshot.scenes[0].candidates[0].key,
                "prompt": "a young woman and a kitten",
            }
        )
    )
    original = services.ImageService.generate

    def uncertain(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError("Response unavailable after local persistence")

    monkeypatch.setattr(services.ImageService, "generate", uncertain)
    with pytest.raises(OSError):
        live.improve(report, request)
    assert live.load(report).improvements[0].run_id
    assert stub.submissions == 5
    live.improve(report, request)
    assert stub.submissions == 5


def test_selection_cannot_reference_another_scene_or_unknown_image(completion):
    report, _, _ = completion
    live.baseline(report)
    reviews_for(report)
    payload = json.loads((report / "reviews.json").read_bytes())
    payload["selections"][0]["key"] = f"runs/{uuid4()}/images/0001.png"
    (report / "reviews.json").write_text(json.dumps(payload))
    publication.publish(report)
    selected = json.loads((report / "manifest.json").read_bytes())["selections"]
    assert selected[0]["source"] == "rule"
    assert selected[0]["key"] != payload["selections"][0]["key"]


def test_confirmed_failure_does_not_block_later_scenes_and_retry_uses_budget(
    completion, client, stub, monkeypatch
):
    report, _, _ = completion
    original = client.submit

    def fail_first(*args):
        identity = original(*args)
        if stub.submissions == 1:
            stub.history[identity]["status"] = {"status_str": "error", "completed": True}
        return identity

    monkeypatch.setattr(client, "submit", fail_first)
    from typer import Exit

    with pytest.raises(Exit):
        live.baseline(report)
    case = live.load(report)
    snapshot = Client(live.project(report).root).stories.show(case.story_id)
    assert [scene.state for scene in snapshot.scenes] == ["failed", "saved", "saved", "saved"]
    assert stub.submissions == 4 and case.reserved_outputs == 4
    live.baseline(report, retry_failed=True)
    assert stub.submissions == 5 and live.load(report).reserved_outputs == 5
    assert Client(live.project(report).root).stories.show(case.story_id).complete
    with pytest.raises(RuntimeError):
        live.baseline(report, retry_failed=True)


@pytest.mark.parametrize(
    "payload", [None, "invalid JSON", "incomplete", '{"format_version":2,"status":"timeout"}']
)
def test_missing_invalid_or_timed_out_review_selects_saved_images(completion, payload):
    report, _, _ = completion
    live.baseline(report)
    if payload == "incomplete":
        reviews_for(report)
        value = json.loads((report / "reviews.json").read_bytes())
        for row in value["reviews"]:
            row["decision"] = "accept"
            row["checks"] = row["checks"][:1]
            row["checks"][0]["verdict"] = "match"
        payload = json.dumps(value)
    if payload is not None:
        (report / "reviews.json").write_text(payload, encoding="utf-8")
    publication.publish(report)
    manifest = json.loads((report / "manifest.json").read_bytes())
    assert manifest["selection_complete"]
    assert all(row["source"] == "rule" for row in manifest["selections"])
    assert not manifest["selection_is_quality_approval"]
    assert not Client(live.project(report).root).evaluations.list()


def test_selection_survives_deleted_derived_manifest(completion):
    report, _, _ = completion
    live.baseline(report)
    publication.publish(report)
    previous = live.load(report).selections
    (report / "manifest.json").unlink()
    publication.publish(report)
    assert live.load(report).selections == previous


def test_reference_edit_freezes_its_parent_and_reconciles_without_resubmission(completion, stub):
    report, _, _ = completion
    live.baseline(report)
    case = live.load(report)
    api = Client(live.project(report).root)
    parent = api.stories.show(case.story_id).scenes[0].candidates[0].key
    original = api.artifact_path(parent).read_bytes()
    request = report / "edit.json"
    request.write_text(
        json.dumps(
            {
                "scene": 1,
                "method": "edit",
                "parent_key": parent,
                "prompt": "Keep the kitten and umbrella, show the woman's yellow raincoat.",
            }
        ),
        encoding="utf-8",
    )
    live.improve(report, request)
    job = live.load(report).improvements[0]
    assert set(job.inputs) == {"reference"}
    assert job.inputs["reference"].sha256
    run = live.project(report).repository.read(job.run_id)
    assert run.compiled_input.inputs["reference"] == job.inputs["reference"]
    assert api.artifact_path(parent).read_bytes() == original
    live.improve(report, request)
    assert stub.submissions == 5
    assert live.load(report).reserved_outputs == 5


def test_corrupt_image_is_ineligible_without_rewriting_saved_run(completion):
    report, _, _ = completion
    live.baseline(report)
    case = live.load(report)
    api = Client(live.project(report).root)
    snapshot = api.stories.show(case.story_id)
    api.artifact_path(snapshot.scenes[0].candidates[0].key).write_bytes(b"corrupt")
    publication.publish(report)
    manifest = json.loads((report / "manifest.json").read_bytes())
    assert not manifest["selection_complete"] and not manifest["comic_complete"]
    assert len(manifest["unavailable_images"]) == 1
    assert api.stories.show(case.story_id).complete
