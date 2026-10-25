"""Verify story evidence on an explicitly started GPU and release ownership."""

import hashlib
import json
import os
import subprocess
import sys
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
    os.environ.get("KOMAQUILL_EVIDENCE_ACCEPTANCE") != "1",
    reason="Requires an explicitly started GPU and a reviewed four-scene plan",
)
def test_story_evidence_and_shutdown():
    project = find_project()
    api = Client(project.root)
    control = Colab(project)
    receipt = control.receipt()
    assert receipt is not None and control.refresh(receipt)
    folder = project.reports / "story-evidence/gpu" / uuid4().hex
    folder.mkdir(parents=True)
    result = {
        "format_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "identity": project.identity(),
        "runtime_id": str(receipt.runtime_id),
        "checks": [],
    }
    try:
        story_id = UUID(os.environ["KOMAQUILL_ACCEPTANCE_STORY"])
        command = [
            sys.executable,
            "-B",
            "-m",
            "komaquill",
            "stories",
            "render",
            str(story_id),
            "--seed",
            "42",
            "--json",
        ]
        image_profile = os.environ.get("KOMAQUILL_ACCEPTANCE_IMAGE_PROFILE")
        if image_profile:
            command += ["--image-profile", image_profile]
        result["image_profile"] = image_profile
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )
        (folder / "cli.stdout.jsonl").write_text(process.stdout, encoding="utf-8")
        (folder / "cli.stderr.txt").write_text(process.stderr, encoding="utf-8")
        result["cli_exit_code"] = process.returncode
        assert process.returncode == 0
        shown = json.loads(process.stdout)
        final = api.stories.show(story_id)
        assert final.complete and final.finished and final.image_count == 4 and final.render_id
        assert shown["complete"] and shown["finished"]
        result.update(story_id=str(story_id), render_id=str(final.render_id))
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
        output = FileStoryRepository(project).folder(story_id) / "renders" / str(final.render_id)
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        assert [row["position"] for row in manifest["scenes"]] == [1, 2, 3, 4]
        assert sorted(path.name for path in (output / "images").iterdir()) == [
            f"{position:03d}-001-001.png" for position in range(1, 5)
        ]
        assert "images/001-001-001.png" in (output / "story.md").read_text(encoding="utf-8")
        before = set(run.run_id for run in project.repository.load())
        resumed = list(api.stories.resume(final.render_id))
        assert len(resumed) == 1 and resumed[0].kind == "finished" and resumed[0].snapshot.complete
        assert before == set(run.run_id for run in project.repository.load())
        summary = api.stories.export(story_id)
        full = api.stories.export(story_id, detail="full")
        assert summary.planning == full.planning
        assert summary.render == full.render and summary.selected_run_ids == full.selected_run_ids
        assert not summary.objects and full.objects
        if summary.planning.conditions.get("provider") != "fake":
            assert summary.planning.usage.total_tokens is not None
        for run in summary.runs:
            for image in run.images:
                assert (project.root / image.workspace_path).is_file()
        stamp = manifest_path.stat().st_mtime_ns
        time.sleep(receipt.settings.poll_interval * 2 + 1)
        api.stories.refresh(story_id)
        assert manifest_path.stat().st_mtime_ns == stamp
        (folder / "summary.json").write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        result["checks"] = [
            "cli_render",
            "sequential_save",
            "image_hashes",
            "ordered_manifest",
            "resume_without_resubmit",
            "summary_full_parity",
            "image_references",
            "unchanged_manifest",
        ]
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
    result["checks"].extend(["provider_stopped", "monitor_stopped", "port_released"])
    (folder / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
