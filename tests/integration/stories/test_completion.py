"""Verify completion controls and evidence using FakeListLLM and the ComfyUI stub."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from PIL import Image

from komaquill.api import Client, services, stories
from komaquill.infrastructure.asset_store import model_path
from tests.live import completion_report as publication
from tests.live import story_completion as live
from tests.support.stories import planned


@pytest.fixture
def completion(project, session, client, monkeypatch):
    repository, plan = planned(project)
    model = project.settings().assets["pony"]
    path = model_path(project.models, model)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"Test-only model placeholder; never loaded by a GPU.")
    monkeypatch.setattr(live, "verified_cache", lambda *args: True)
    report = project.root / "completion"
    case = live.prepare(project.root, plan.request.story_id, report)
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
        json.dumps({"format_version": 1, "reviews": reviews, "selections": selections})
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
    assert all(len(scene.candidates) == 2 for scene in snapshot.scenes)
    publication.publish(report)
    manifest = json.loads((report / "manifest.json").read_bytes())
    assert manifest["baseline_complete"] and manifest["selection_complete"]
    assert manifest["selection_is_quality_approval"] is False
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
    assert len(Client(live.project(report).root).evaluations.list()) == 8
    live.baseline(report)
    assert stub.submissions == 4
    assert live.load(report).reserved_outputs == 8


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
    assert first.reserved_outputs == 10
    assert first.improvements[0].run_id not in snapshot.scenes[2].attempt_ids
    assert stub.submissions == 5
    live.improve(report, request)
    assert stub.submissions == 5 and live.load(report).reserved_outputs == 10
    publication.publish(report)
    manifest = json.loads((report / "manifest.json").read_bytes())
    assert len(manifest["images"]) == 10
    assert len(manifest["selections"]) == 4
    assert (report / "baseline-selection.md").read_bytes() == original_selection
    assert (report / "before-after.md").is_file()


def test_budget_and_deadline_are_not_reset_on_resume(completion):
    report, _, _ = completion
    case = live.load(report)
    live.reserve(case, 12)
    live.save(report, case)
    with pytest.raises(RuntimeError):
        live.reserve(live.load(report), 1)
    case.gpu_started_at = datetime.now(UTC) - timedelta(seconds=case.send_seconds)
    live.save(report, case)
    with pytest.raises(RuntimeError):
        live.sending_allowed(live.load(report))
    with pytest.raises(RuntimeError):
        live.start(report)


def test_unresolved_baseline_is_never_automatically_resubmitted(completion, stub):
    report, _, _ = completion
    case = live.load(report)
    case.baseline_reserved = True
    case.reserved_outputs = 8
    live.save(report, case)
    with pytest.raises(RuntimeError):
        live.baseline(report)
    assert stub.submissions == 0


def test_frozen_config_error_does_not_block_owned_gpu_shutdown(completion, monkeypatch):
    report, _, _ = completion
    (report / "workspace/komaquill.toml").write_text("corrupt")
    with pytest.raises(RuntimeError):
        live.load(report)
    stopped = []
    monkeypatch.setattr(services.SessionService, "stop", lambda self, **kw: stopped.append(kw))
    live.stop(report)
    assert len(stopped) == 1
    (report / "case.json").write_text("{}")
    live.stop(report)
    assert stopped[-1]["force"]


def test_repair_graph_declares_only_base_mask_and_standard_preservation_path(completion):
    report, _, _ = completion
    profile = live.project(report).settings().presets["completion-repair"]
    assert set(profile.image_inputs) == {"base", "mask"}
    assert profile.asset_ids == ("pony",) and not profile.extensions
    assert profile.api["3"]["inputs"]["latent_image"] == ["37", 0]
    assert profile.api["33"]["class_type"] == "VAEEncode"
    assert profile.api["35"]["inputs"]["destination"] == ["40", 0]
    assert profile.api["35"]["inputs"]["source"] == ["8", 0]
    assert profile.api["35"]["inputs"]["mask"] == ["31", 0]
    assert profile.parameters["denoise"] == 0.55
    assert all("Crop" not in node["class_type"] for node in profile.api.values())


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
    with pytest.raises(ValueError):
        publication.publish(report)


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
    assert stub.submissions == 4 and case.reserved_outputs == 8
    live.baseline(report, retry_failed=True)
    assert stub.submissions == 5 and live.load(report).reserved_outputs == 10
    assert Client(live.project(report).root).stories.show(case.story_id).complete
    with pytest.raises(RuntimeError):
        live.baseline(report, retry_failed=True)


def test_local_repair_preserves_parent_and_freezes_imported_mask(completion, stub):
    report, _, _ = completion
    live.baseline(report)
    snapshot = reviews_for(report)
    api = Client(live.project(report).root)
    parent = snapshot.scenes[2].candidates[0].key
    before = api.artifact_path(parent).read_bytes()
    Image.new("L", (8, 8), "white").save(report / "mask.png")
    request = report / "repair.json"
    request.write_text(
        json.dumps(
            {
                "scene": 3,
                "method": "inpaint",
                "parent_key": parent,
                "prompt": "natural paws resting on the pavement",
                "mask": "mask.png",
            }
        )
    )
    live.improve(report, request)
    job = live.load(report).improvements[0]
    assert job.parent_key == parent and set(job.inputs) == {"base", "mask"}
    assert api.artifact_path(parent).read_bytes() == before
    run = live.project(report).repository.read(job.run_id)
    graph = json.loads(api.artifact_path(f"runs/{run.run_id}/workflow-api.json").read_bytes())
    assert graph["3"]["inputs"]["denoise"] == 0.55
    assert stub.submissions == 5
