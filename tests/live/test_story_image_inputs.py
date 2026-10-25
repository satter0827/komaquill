"""Compare explicit local planning and bounded Pony conditioning on retained inputs."""

import json
import os
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from komaquill.api import Client, KomaquillError
from komaquill.infrastructure.configuration import find_project
from komaquill.infrastructure.stories.storage import FileStoryRepository
from tests.support.stories import source
from tests.support.story_evidence import planning_measurement


@pytest.mark.skipif(
    os.environ.get("KOMAQUILL_REAL_LLM") != "1", reason="Requires explicit local 4B authorization"
)
@pytest.mark.parametrize("count", [4, "auto"])
def test_local_4b_visual_plan(count, monkeypatch):
    project = find_project()
    api = Client(project.root)
    monkeypatch.setenv("KOMAQUILL_STORY_PURPOSE", "verification")
    monkeypatch.setenv("KOMAQUILL_STORY_CASE_ID", "rainy-cat-visual-v2")
    started = datetime.now(UTC).isoformat()
    before = time.monotonic()
    plan = None
    failure = None
    story_id = None
    try:
        plan = api.stories.plan(
            source(),
            source_name="rainy-cat-visual.txt",
            images=count,
            llm_profile="lmstudio-4b",
            image_profile="pony-story",
            allow_real_llm=True,
        )
        story_id = plan.request.story_id
    except KomaquillError as error:
        failure = error
        story_id = UUID(error.details["story_id"]) if "story_id" in error.details else None
    import hashlib

    record = planning_measurement(
        api,
        story_id,
        case_id="rainy-cat-visual-v2",
        count=count,
        source_sha256=hashlib.sha256(source().encode()).hexdigest(),
        started_at=started,
        elapsed_seconds=time.monotonic() - before,
        failure=failure,
    )
    folder = project.reports / "story-images/planning"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{count}-{story_id or uuid4()}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if failure:
        raise failure
    assert plan is not None and plan.format_version == 2
    assert all(scene.subjects is not None and len(scene.subjects) <= 2 for scene in plan.scenes)


@pytest.mark.skipif(
    os.environ.get("KOMAQUILL_IMAGE_COMPARISON") != "1",
    reason="Requires explicit T4 authorization and a reviewed saved plan",
)
def test_pony_global_and_regional_comparison():
    from komaquill.infrastructure.colab import Colab, check_port
    from komaquill.infrastructure.colab_bridge import observe, provider
    from komaquill.infrastructure.monitor_state import alive, read_monitor

    project = find_project()
    api = Client(project.root)
    repository = FileStoryRepository(project)
    plan = repository.read(UUID(os.environ["KOMAQUILL_VISUAL_PLAN"]))
    baseline = repository.read(UUID(os.environ["KOMAQUILL_VISUAL_BASELINE"]))
    assert plan.request.source_sha256 == baseline.request.source_sha256
    assert len(plan.scenes) == len(baseline.scenes) == 4
    control = Colab(project)
    assert control.receipt() is None and not api.sessions.status().collector_running
    folder = project.reports / "story-images/gpu" / uuid4().hex
    folder.mkdir(parents=True)
    report = {
        "format_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "identity": project.identity(),
        "story_id": str(plan.request.story_id),
        "baseline_story_id": str(baseline.request.story_id),
        "pairs": [],
        "fixed_conditions": ["model", "seed", "sampling", "quality", "source"],
        "criteria": [
            "subject_count_and_kind",
            "appearance",
            "prop_holder_and_state",
            "action",
            "sequence",
        ],
        "semantic_review": "manual; saved is not proof of image quality",
    }
    receipt = None
    started = time.monotonic()
    succeeded = False
    try:
        api.sessions.start(profiles=["pony", "pony-story"])
        receipt = control.receipt()
        assert receipt is not None
        profiles = receipt.settings.presets
        for key in (
            "checkpoint",
            "width",
            "height",
            "steps",
            "cfg",
            "sampler",
            "scheduler",
            "clip_skip",
            "quality_enabled",
            "positive_prefix",
            "negative_prefix",
            "negative",
        ):
            assert getattr(profiles["pony"], key) == getattr(profiles["pony-story"], key)
        characters = {character.key: character for character in baseline.characters}
        for position in (0, 2):
            old = baseline.scenes[position]
            scene = plan.scenes[position]
            assert scene.subjects is not None
            assert set(scene.source_refs) == set(old.source_refs)
            from komaquill.application.stories.image_inputs import scene_input

            specification = scene_input(plan, scene, profiles["pony-story"])
            old_prompt = "\n".join(
                [
                    *(characters[key].appearance for key in old.characters),
                    old.visual_description,
                    str(baseline.planner.planning_policy.get("style", "")),
                ]
            )
            for seed in (42, 31415):
                for label, prompt, subjects, profile in (
                    ("A", old_prompt, (), "pony"),
                    ("B", specification.prompt, specification.subject_prompts, "pony"),
                    ("C", specification.prompt, specification.subject_prompts, "pony-story"),
                ):
                    assert time.monotonic() - started < 1800
                    run = api.images.generate(prompt, subjects=subjects, seed=seed, profile=profile)
                    assert run.state == "saved" and run.compiled_input is not None
                    report["pairs"].append(
                        {
                            "scene": position + 1,
                            "seed": seed,
                            "condition": label,
                            "run_id": str(run.run_id),
                            "input": run.compiled_input.model_dump(mode="json"),
                            "images": [f"runs/{run.run_id}/{name}" for name in run.images],
                            "workflow_sha256": run.workflow_sha256,
                        }
                    )
                    (folder / "comparison.json").write_text(
                        json.dumps(report, indent=2), encoding="utf-8"
                    )
        if os.environ.get("KOMAQUILL_VISUAL_REVIEW") == "1":
            report["phase"] = "awaiting_review"
            (folder / "comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            decision_file = folder / "decision.json"
            deadline = min(started + 1800, time.monotonic() + 180)
            while not decision_file.is_file():
                assert time.monotonic() < deadline, (
                    "No reviewed candidate was selected before the limit"
                )
                time.sleep(1)
            decision = json.loads(decision_file.read_bytes())
            assert decision["profile"] in {"pony", "pony-story", None}
            report["review"] = decision
            if decision["profile"] is not None:
                events = list(
                    api.stories.render(
                        plan.request.story_id, image_profile=decision["profile"], seed=42
                    )
                )
                snapshot = events[-1].snapshot
                assert snapshot.complete and snapshot.render_id
                assert any(event.snapshot.counts.get("saved") == 1 for event in events)
                report["render_id"] = str(snapshot.render_id)
                report["story_images"] = [scene.primary_image.key for scene in snapshot.scenes]
        succeeded = True
    finally:
        try:
            api.sessions.stop(force=not succeeded)
        finally:
            report["phase"] = "finished" if succeeded else "incomplete"
            report["finished_at"] = datetime.now(UTC).isoformat()
            report["runtime_id"] = str(receipt.runtime_id) if receipt else None
            report["remaining_owner"] = control.receipt() is not None
            (folder / "comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert receipt is not None and control.receipt() is None
    observation, assignment = observe(
        provider(), receipt.model_dump(mode="json"), receipt.settings.request_timeout
    )
    assert observation.state == "stopped" and assignment is None
    monitor = read_monitor(project)
    deadline = time.monotonic() + receipt.settings.control_timeout
    while monitor is not None and alive(monitor):
        assert time.monotonic() < deadline
        time.sleep(receipt.settings.poll_interval)
    check_port(receipt.settings.local_port)
