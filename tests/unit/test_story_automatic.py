"""Check automatic scenes, focused repair and preserved planning evidence."""

import json
from importlib.resources import files

import pytest
from langchain_core.language_models.fake import FakeListLLM

from komaquill.application.stories.planning import plan_story
from komaquill.domain.errors import KomaquillError
from komaquill.domain.stories import PlanningResponse
from komaquill.infrastructure.stories.configuration import settings
from komaquill.infrastructure.stories.llm import LangChainPlanner, planner, token_usage
from komaquill.infrastructure.stories.storage import FileStoryRepository
from tests.support.stories import request


@pytest.mark.parametrize("count", ["auto", 1, 4, 8])
def test_auto_or_fixed_count_and_pre_call_evidence(project, count):
    options = settings(project, llm_profile="fake", images=count)
    value = request().model_copy(update={"image_count": count, "max_images": options.max_images})
    repo = FileStoryRepository(project)
    model = planner(project, options, allow_real_llm=False)
    result = plan_story(repo, model, value, max_images=options.max_images)
    assert len(result.scenes) == (4 if count == "auto" else count)
    assert (repo.folder(value.story_id) / "provenance.json").is_file()
    before = json.loads(
        (repo.folder(value.story_id) / "planning/inputs/attempt-01.json").read_bytes()
    )
    assert before["input"]["image_count"] == count
    assert "schema" not in before["input"] and "scene_guide" not in before["input"]
    assert before["schema"]["properties"]["scenes"]["maxItems"] == (8 if count == "auto" else count)


def test_repair_includes_specific_errors_and_the_previous_visible_output(project):
    options = settings(project, llm_profile="fake")
    good = (
        files("komaquill")
        .joinpath("resources/stories/examples/rainy-cat/plan-4.json")
        .read_text(encoding="utf-8")
    )
    bad = json.loads(good)
    bad["scenes"][1]["narrative"] = bad["scenes"][0]["narrative"]
    previous = json.dumps(bad, ensure_ascii=False)
    fake = FakeListLLM(responses=[previous, good])
    base = planner(project, options, allow_real_llm=False)
    repo = FileStoryRepository(project)
    value = request()
    plan_story(repo, LangChainPlanner(base.provenance, fake), value, max_images=8)
    second = json.loads(
        (repo.folder(value.story_id) / "planning/inputs/attempt-02.json").read_bytes()
    )
    correction = json.loads(second["input"]["correction"])
    assert correction["previous_output"] == previous
    assert "repeated prose" in str(correction["validation_errors"])
    assert len(list((repo.folder(value.story_id) / "planning").glob("[0-9]*.json"))) == 2


def test_truncation_is_not_repaired_and_failed_input_is_preserved(project):
    base = planner(project, settings(project, llm_profile="fake"), allow_real_llm=False)
    calls = []

    class Truncated:
        provenance = base.provenance

        def complete(self, *args):
            calls.append(args)
            return PlanningResponse(content="{", finish_reason="length")

    repo = FileStoryRepository(project)
    value = request()
    with pytest.raises(KomaquillError):
        plan_story(repo, Truncated(), value, max_images=8)
    assert len(calls) == 1
    assert (repo.folder(value.story_id) / "provenance.json").exists()
    assert not (repo.folder(value.story_id) / "plan.json").exists()


def test_provider_counters_are_not_estimated_or_summed_when_missing():
    assert token_usage(None) is None
    actual = token_usage(
        {
            "prompt_tokens": 20,
            "completion_tokens": 7,
            "total_tokens": 27,
            "completion_tokens_details": {"reasoning_tokens": 2},
        }
    )
    assert actual.input_tokens == 20 and actual.reasoning_tokens == 2
    partial = token_usage({"input_tokens": 12, "output_tokens": False})
    assert (
        partial.input_tokens == 12
        and partial.total_tokens is None
        and partial.output_tokens is None
    )


def test_local_policy_replacement_and_explicit_overrides(project, monkeypatch):
    import shutil
    from pathlib import Path

    target = project.root / "presets/custom"
    shutil.copytree(Path(str(files("komaquill").joinpath("resources/presets/pony"))), target)
    (target / "planning.toml").write_text('style="local style"\nguidance=["Use close-ups."]\n')
    (project.root / "komaquill.toml").write_text(
        '[story]\nimage_profile="custom"\n[story.planning]\nstyle="workspace style"\n'
    )
    assert settings(project).planning.style == "workspace style"
    monkeypatch.setenv("KOMAQUILL_STORY_PLANNING__STYLE", "environment style")
    options = settings(project, planning={"style": "explicit style"})
    assert options.planning.style == "explicit style" and options.planning.guidance == [
        "Use close-ups."
    ]


def test_failed_plan_can_be_exported_without_guessing_a_successful_plan(project):
    from komaquill.api import Client
    from tests.support.stories import source

    client = Client(project.root)
    with pytest.raises(KomaquillError):
        client.stories.plan(source() + "extra", llm_profile="fake")
    path = next((project.data / "stories").glob("*/request.json"))
    from uuid import UUID

    evidence = client.stories.export(UUID(json.loads(path.read_bytes())["story_id"]))
    assert evidence["story"] is None and evidence["plan"] is None
    assert evidence["planning"]["provenance"]["provider"] == "fake"
    assert evidence["planning"]["attempts"][0]["failure"]["code"] == "fake_story_mismatch"
