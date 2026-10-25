"""Check story validation and bounded repair with LangChain's FakeListLLM."""

import hashlib
import json
from datetime import datetime
from importlib.resources import files

import pytest
from langchain_core.language_models.fake import FakeListLLM
from pydantic import ValidationError

from komaquill.application.stories.planning import plan_story
from komaquill.application.stories.ports import output_schema
from komaquill.domain.errors import KomaquillError
from komaquill.domain.stories import PlanningResponse, StoryDraft, StoryInput, validate_draft
from komaquill.infrastructure.stories.configuration import settings
from komaquill.infrastructure.stories.llm import LangChainPlanner, planner
from komaquill.infrastructure.stories.storage import FileStoryRepository
from tests.support.stories import planned, request, source


@pytest.mark.parametrize("count", range(1, 9))
def test_fake_plans_are_coherent_and_immutable(project, count):
    repository, result = planned(project, count)
    assert len(result.scenes) == count
    assert [scene.position for scene in result.scenes] == list(range(1, count + 1))
    assert len({scene.scene_id for scene in result.scenes}) == count
    assert result.request.source_text == source()
    assert "ミオ" in "".join(scene.narrative for scene in result.scenes)
    assert result.planner.provider == "fake"
    assert (
        result.planner.instruction_sha256
        == hashlib.sha256(result.planner.instructions.encode()).hexdigest()
    )
    assert repository.read(result.request.story_id) == result
    with pytest.raises(KomaquillError):
        repository.complete(result)
    with pytest.raises(FileExistsError):
        repository.begin(result.request)


@pytest.mark.parametrize(
    "fault", ["json", "count", "character", "source", "blank", "coverage", "duplicate", "language"]
)
def test_invalid_response_repairs_once_through_same_pipeline(project, fault):
    repository = FileStoryRepository(project)
    options = settings(project, llm_profile="fake")
    base = planner(project, options, allow_real_llm=False)
    valid = (
        files("komaquill")
        .joinpath("resources/stories/examples/rainy-cat/plan-4.json")
        .read_text(encoding="utf-8")
    )
    broken = json.loads(valid)
    if fault == "language":
        broken["scenes"][0]["narrative"] = "Mio covers the kitten with her umbrella."
    elif fault == "count":
        broken["scenes"].pop()
    elif fault == "character":
        broken["scenes"][0]["subjects"][0]["character"] = "c99"
    elif fault == "source":
        broken["scenes"][0]["source_refs"] = ["missing"]
    elif fault == "duplicate":
        for key in ("visual_description", "location", "subjects", "props"):
            broken["scenes"][1][key] = broken["scenes"][0][key]
    elif fault == "coverage":
        broken["scenes"][-1]["source_refs"] = ["p3"]
    elif fault == "blank":
        broken["scenes"][0]["narrative"] = " "
    model = FakeListLLM(responses=["{" if fault == "json" else json.dumps(broken), valid])
    actual = LangChainPlanner(base.provenance, model)
    value = request()
    result = plan_story(repository, actual, value, max_images=8)
    attempts = list((repository.folder(value.story_id) / "planning").glob("*.json"))
    assert result.request == value and len(attempts) == 2
    first = json.loads((repository.folder(value.story_id) / "planning/01.json").read_bytes())
    assert first["failure"]["code"] == "story_plan_invalid"
    with pytest.raises(KomaquillError):
        from komaquill.domain.stories import PlanningAttempt

        repository.attempt(value.story_id, PlanningAttempt.model_validate(first))


def test_repair_budget_does_not_loop_over_cycling_fake(project):
    options = settings(project, llm_profile="fake")
    base = planner(project, options, allow_real_llm=False)
    model = FakeListLLM(responses=["{", "{", "{}"])
    repository = FileStoryRepository(project)
    value = request()
    with pytest.raises(KomaquillError) as error:
        plan_story(repository, LangChainPlanner(base.provenance, model), value, max_images=8)
    assert error.value.code == "story_plan_invalid" and model.i == 2
    assert not (repository.folder(value.story_id) / "plan.json").exists()


def test_real_permission_and_limits_are_checked_before_writes(project):
    options = settings(project)
    with pytest.raises(KomaquillError) as error:
        planner(project, options, allow_real_llm=False)
    assert error.value.code == "real_llm_not_allowed"
    base = planner(project, settings(project, llm_profile="fake"), allow_real_llm=False)
    real = LangChainPlanner(base.provenance.model_copy(update={"provider": "openai-compatible"}))
    repository = FileStoryRepository(project)
    with pytest.raises(KomaquillError):
        plan_story(repository, real, request(), max_images=8)
    with pytest.raises(KomaquillError):
        plan_story(repository, base, request(9), max_images=8)
    with pytest.raises(KomaquillError):
        plan_story(repository, base, request(), max_images=8, repair_attempts=2)
    assert not repository.root.exists()


def test_truncation_is_recorded_without_silent_scene_loss(project):
    base = planner(project, settings(project, llm_profile="fake"), allow_real_llm=False)

    class Truncated:
        provenance = base.provenance

        def complete(self, *args):
            return PlanningResponse(content="{}", finish_reason="length")

    repository = FileStoryRepository(project)
    value = request()
    with pytest.raises(KomaquillError):
        plan_story(repository, Truncated(), value, max_images=8, repair_attempts=0)
    attempt = json.loads(next((repository.folder(value.story_id)).rglob("01.json")).read_bytes())
    assert attempt["response"]["finish_reason"] == "length"


def test_invalid_source_and_references_are_rejected():
    with pytest.raises(ValidationError):
        StoryInput(source_name="x", source_text="story", source_sha256="0" * 64, image_count=1)
    value = request()
    data = json.loads(
        files("komaquill").joinpath("resources/stories/examples/rainy-cat/plan-4.json").read_bytes()
    )
    data["characters"].append(data["characters"][0])
    with pytest.raises(ValueError):
        validate_draft(StoryDraft.model_validate(data), value)
    data["characters"].pop()
    data["scenes"][0]["subjects"] = [data["scenes"][0]["subjects"][0]] * 2
    with pytest.raises(ValueError):
        validate_draft(StoryDraft.model_validate(data), value)
    with pytest.raises(ValidationError):
        value.model_validate({**value.model_dump(), "created_at": datetime(2026, 1, 1)})
    schema = output_schema(value)
    assert schema["properties"]["scenes"]["minItems"] == value.image_count


def test_fake_rejects_unrelated_source_and_backend_failure_is_recorded(project):
    repository = FileStoryRepository(project)
    base = planner(project, settings(project, llm_profile="fake"), allow_real_llm=False)
    value = request(text="An unrelated private story.")
    with pytest.raises(KomaquillError) as error:
        plan_story(repository, base, value, max_images=8)
    assert error.value.code == "fake_story_mismatch"
    attempt = json.loads(next((repository.folder(value.story_id)).rglob("01.json")).read_bytes())
    assert attempt["failure"]["code"] == "fake_story_mismatch"


def test_plan_checksum_prevents_later_manual_edits(project):
    repository, result = planned(project)
    path = repository.folder(result.request.story_id) / "plan.json"
    data = json.loads(path.read_bytes())
    data["plan"]["title"] = "Changed"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(KomaquillError):
        repository.read(result.request.story_id)
