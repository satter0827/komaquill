"""Verify a saved plan on one explicitly started GPU, always releasing ownership."""

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from komaquill.api import Client
from komaquill.infrastructure.colab import Colab, check_port
from komaquill.infrastructure.colab_bridge import observe, provider
from komaquill.infrastructure.configuration import find_project
from komaquill.infrastructure.monitor_state import alive, read_monitor
from komaquill.infrastructure.stories.storage import FileStoryRepository


@pytest.mark.skipif(
    os.environ.get("KOMAQUILL_STORY_ACCEPTANCE") != "1",
    reason="Requires an explicitly started GPU and a reviewed four-scene plan",
)
def test_saved_story_render_and_order_comparison():
    project = find_project()
    api = Client(project.root)
    control = Colab(project)
    receipt = control.receipt()
    assert receipt is not None and control.refresh(receipt)
    folder = project.reports / "story-workflow/gpu" / uuid4().hex
    folder.mkdir(parents=True)
    started = datetime.now(UTC).isoformat()
    result = {
        "format_version": 1,
        "started_at": started,
        "identity": project.identity(),
        "runtime_id": str(receipt.runtime_id),
        "changed_condition": "appearance before/after focal scene description",
        "fixed_conditions": ["plan", "seed", "model", "sampler", "steps", "conditioning"],
        "pairs": [],
    }
    try:
        story_id = UUID(os.environ["KOMAQUILL_ACCEPTANCE_STORY"])
        plan = FileStoryRepository(project).read(story_id)
        events = list(api.stories.render(story_id, seed=42))
        final = events[-1].snapshot
        assert final.complete and final.image_count == 4 and final.render_id
        assert any(event.snapshot.counts.get("saved") == 1 for event in events)
        result.update(story_id=str(story_id), render_id=str(final.render_id))
        render = FileStoryRepository(project).render(final.render_id)
        runs = [project.repository.read(scene.run_id) for scene in final.scenes]
        assert runs[0].saved_at < runs[-1].created_at
        for scene in final.scenes:
            assert scene.primary_image is not None and scene.primary_image.metadata
            target = api.artifact_path(scene.primary_image.key)
            with target.open("rb") as image:
                assert (
                    hashlib.file_digest(image, "sha256").hexdigest()
                    == scene.primary_image.metadata.sha256
                )
        work = FileStoryRepository(project).folder(story_id)
        output = work / "renders" / str(final.render_id)
        manifest = json.loads((output / "manifest.json").read_bytes())
        assert [row["position"] for row in manifest["scenes"]] == [1, 2, 3, 4]
        assert sorted(path.name for path in (output / "images").iterdir()) == [
            f"{position:03d}-001-001.png" for position in range(1, 5)
        ]
        assert "images/001-001-001.png" in (output / "story.md").read_text(encoding="utf-8")
        before = set(run.run_id for run in project.repository.load())
        assert list(api.stories.resume(final.render_id))[-1].snapshot.complete
        assert before == set(run.run_id for run in project.repository.load())
        characters = {character.key: character for character in plan.characters}
        for position in (0, 2):
            scene = plan.scenes[position]
            prompt = "\n".join(
                [
                    scene.visual_description,
                    *(characters[key].appearance for key in scene.characters),
                    str(plan.planner.planning_policy["style"]),
                ]
            )
            candidate = api.images.generate(
                prompt, seed=render.scenes[position].seed, profile="klein-4b"
            )
            assert candidate.state == "saved"
            result["pairs"].append(
                {
                    "scene": position + 1,
                    "seed": render.scenes[position].seed,
                    "baseline_run_id": str(runs[position].run_id),
                    "candidate_run_id": str(candidate.run_id),
                    "baseline_images": final.scenes[position].primary_image.key,
                    "candidate_images": [
                        f"runs/{candidate.run_id}/{image}" for image in candidate.images
                    ],
                }
            )
        result["generation_result"] = "saved"
    finally:
        (folder / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        api.sessions.stop()
        result["shutdown"] = json.loads(
            (project.logs / str(receipt.runtime_id) / "shutdown.json").read_bytes()
        )
        result["finished_at"] = datetime.now(UTC).isoformat()
        (folder / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    assert control.receipt() is None
    observation, assignment = observe(
        provider(), receipt.model_dump(mode="json"), receipt.settings.request_timeout
    )
    assert observation.state == "stopped" and assignment is None
    monitor = read_monitor(project)
    deadline = time.monotonic() + receipt.settings.control_timeout
    assert monitor is not None
    while alive(monitor):
        assert time.monotonic() < deadline
        time.sleep(receipt.settings.poll_interval)
    check_port(receipt.settings.local_port)
    assert result["shutdown"]["gpu"] == "stopped" and not result["shutdown"]["errors"]
