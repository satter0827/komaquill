"""Compare explicitly authorized 4B planning with immutable, attributable evidence."""

import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from importlib.resources import files
from uuid import UUID, uuid4

import pytest

from komaquill.api import Client, KomaquillError
from komaquill.infrastructure.configuration import find_project
from tests.support.story_evidence import CASES, planning_measurement


@pytest.fixture(scope="module")
def comparison_report():
    project = find_project()
    folder = (
        project.reports
        / "story-workflow/live"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex)
    )
    folder.mkdir(parents=True)
    (folder / "context.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "identity": project.identity(),
                "created_at": datetime.now(UTC).isoformat(),
                "fixed_conditions": {"llm_profile": "lmstudio-4b", "image_profile": "pony"},
                "changed_condition": "image_count",
                "criteria": [
                    "source events and ending",
                    "no repeated prose",
                    "simple drawable moments",
                    "supplements separated",
                ],
                "semantic_review": "manual; structure is not proof of meaning",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return folder


@pytest.mark.skipif(
    os.environ.get("KOMAQUILL_REAL_LLM") != "1",
    reason="Requires explicit authorization for the local 4B model",
)
@pytest.mark.parametrize("count", [4, "auto"])
@pytest.mark.parametrize("case_id", CASES)
def test_real_4b_fixed_and_auto(comparison_report, monkeypatch, count, case_id):
    text = CASES[case_id]
    if text is None:
        text = (
            files("komaquill")
            .joinpath("resources/stories/examples/rainy-cat/source.md")
            .read_text(encoding="utf-8")
        )
    monkeypatch.setenv("KOMAQUILL_STORY_PURPOSE", "verification")
    monkeypatch.setenv("KOMAQUILL_STORY_CASE_ID", case_id)
    api = Client(os.environ["KOMAQUILL_PROJECT_DIR"])
    started = datetime.now(UTC).isoformat()
    before = time.monotonic()
    plan = None
    failure = None
    story_id = None
    try:
        plan = api.stories.plan(
            text,
            source_name=case_id + ".txt",
            images=count,
            llm_profile="lmstudio-4b",
            allow_real_llm=True,
        )
        story_id = plan.request.story_id
    except KomaquillError as error:
        failure = error
        story_id = UUID(error.details["story_id"]) if "story_id" in error.details else None
    result = planning_measurement(
        api,
        story_id,
        case_id=case_id,
        count=count,
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        started_at=started,
        elapsed_seconds=time.monotonic() - before,
        failure=failure,
    )
    (comparison_report / f"{case_id}-{count}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if failure:
        raise failure
    assert plan is not None
    assert len(plan.scenes) == count if isinstance(count, int) else 1 <= len(plan.scenes) <= 8
    assert all(
        scene.source_refs and re.search(r"[\u3040-\u30ff\u3400-\u9fff]", scene.narrative)
        for scene in plan.scenes
    )
    assert plan.planner.configured_model == "qwen3.5-4b-uncensored-hauhaucs-aggressive"
