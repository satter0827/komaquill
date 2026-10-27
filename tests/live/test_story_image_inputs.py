"""Verify explicitly authorized local planning on retained inputs."""

import json
import os
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from komaquill.api import Client, KomaquillError
from komaquill.infrastructure.configuration import find_project
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
            model="klein-4b",
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
