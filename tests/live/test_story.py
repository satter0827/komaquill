"""Verify explicitly authorized local-model planning and an existing GPU rendering."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from komaquill.application.stories.rendering import story_view
from komaquill.infrastructure.configuration import find_project
from komaquill.infrastructure.stories.configuration import settings
from komaquill.infrastructure.stories.storage import FileStoryRepository

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    os.environ.get("KOMAQUILL_REAL_LLM") != "1",
    reason="Requires explicit authorization for the local 4B model",
)
@pytest.mark.parametrize("count", [1, 4, 8])
def test_real_4b_story_plan(count):
    project = find_project(Path(os.environ["KOMAQUILL_PROJECT_DIR"]))
    report = project.reports / "story-generation" / uuid4().hex
    report.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(KOMAQUILL_STORY_PURPOSE="verification", KOMAQUILL_STORY_CASE_ID="rainy-cat")
    started = datetime.now(UTC).isoformat()
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "komaquill",
            "--project",
            str(project.root),
            "stories",
            "plan",
            str(ROOT / "src/komaquill/resources/stories/examples/rainy-cat/source.md"),
            "--images",
            str(count),
            "--llm-profile",
            "lmstudio-4b",
            "--allow-real-llm",
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=650,
    )
    (report / f"real-4b-{count}.stdout.json").write_text(result.stdout, encoding="utf-8")
    (report / f"real-4b-{count}.stderr.log").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, f"See {report}/real-4b-{count}.stderr.log"
    view = json.loads(result.stdout)
    assert view["image_count"] == len(view["scenes"]) == count
    repository = FileStoryRepository(project)
    plan = repository.read(UUID(view["story_id"]))
    assert plan.planner.configured_model == settings(project).llm_profiles["lmstudio-4b"].model
    assert plan.request.purpose == "verification"
    assert all(
        scene.source_refs and scene.narrative and scene.visual_description for scene in plan.scenes
    )
    (report / f"real-4b-{count}.result.json").write_text(
        json.dumps(
            {
                "started_at": started,
                "finished_at": datetime.now(UTC).isoformat(),
                "story_id": view["story_id"],
                "images": count,
                "identity": plan.planner.app_identity,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.skipif(
    os.environ.get("KOMAQUILL_STORY_RENDER") is None,
    reason="Requires an explicitly completed story rendering on an existing GPU",
)
def test_real_story_saved_images():
    project = find_project(Path(os.environ["KOMAQUILL_PROJECT_DIR"]))
    repository = FileStoryRepository(project)
    render = repository.render(UUID(os.environ["KOMAQUILL_STORY_RENDER"]))
    view = story_view(repository, project, render.story_id, render.render_id)
    assert view["complete"] and view["counts"] == {"saved": 4}
    for scene in view["scenes"]:
        image = scene["primary_image"]
        assert image and scene["narrative"]
        with project.artifacts_store.path(image["key"]).open("rb") as handle:
            assert hashlib.file_digest(handle, "sha256").hexdigest() == image["metadata"]["sha256"]

    runs = [project.repository.read(UUID(scene["run_id"])) for scene in view["scenes"]]
    assert runs[0].saved_at < runs[-1].created_at
