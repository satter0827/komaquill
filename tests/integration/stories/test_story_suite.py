"""Run diverse paired stories through the same public planning and image services."""

import json
from contextlib import contextmanager

import pytest
from langchain_core.language_models.fake import FakeListLLM

from komaquill.api import Client, services, stories
from komaquill.application.stories.planning import plan_story
from komaquill.infrastructure.stories.configuration import settings
from komaquill.infrastructure.stories.llm import LangChainPlanner, planner
from komaquill.infrastructure.stories.storage import FileStoryRepository
from tests.support.stories import request
from tests.support.story_cases import cases


@pytest.mark.parametrize("case_id", cases())
@pytest.mark.parametrize("count", [4, "auto"])
def test_paired_corpus_uses_common_public_pipeline(
    project, session, client, monkeypatch, case_id, count
):
    case = cases()[case_id]
    options = settings(project, images=count, llm_profile="fake")
    base = planner(project, options, allow_real_llm=False)
    fake = LangChainPlanner(
        base.provenance, FakeListLLM(responses=[case.draft.model_dump_json()], cache=False)
    )
    monkeypatch.setattr(stories, "planner", lambda *args, **kwargs: fake)
    api = Client(project.root)
    plan = api.stories.plan(case.source, images=count, llm_profile="fake")
    assert plan.request.source_sha256 == case.source_sha256
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connected(_):
        yield client, active

    monkeypatch.setattr(stories, "connected", connected)
    monkeypatch.setattr(services, "connected", connected)
    final = list(api.stories.render(plan.request.story_id, seed=424242))[-1].snapshot
    assert final.complete and len(final.scenes) == 4
    assert all(scene.candidates for scene in final.scenes)
    assert [len(scene.compiled_input.subject_prompts) for scene in final.scenes] == [
        len(scene.subjects) for scene in plan.scenes
    ]
    sent = json.dumps(
        json.loads(
            (
                FileStoryRepository(project).folder(plan.request.story_id)
                / "planning/inputs/attempt-01.json"
            ).read_bytes()
        ),
        ensure_ascii=False,
    )
    assert all(item not in sent for item in case.forbidden)
    assert list(api.stories.resume(final.render_id))[-1].snapshot.complete


@pytest.mark.parametrize("case_id", cases())
def test_reference_and_paragraph_identity_do_not_change_visible_conditioning(project, case_id):
    from komaquill.application.stories.image_inputs import scene_input
    from komaquill.domain.stories import StoryDraft

    case = cases()[case_id]
    options = settings(project, images=4, llm_profile="fake")
    base = planner(project, options, allow_real_llm=False)

    def make(value, text=None):
        return plan_story(
            FileStoryRepository(project),
            LangChainPlanner(
                base.provenance, FakeListLLM(responses=[json.dumps(value)], cache=False)
            ),
            request(4, case.source if text is None else text),
            max_images=8,
        )

    original = make(case.draft.model_dump(mode="json"))
    value = case.draft.model_dump(mode="json")
    mapping = {row["key"]: "visible_" + row["key"] for row in value["characters"] + value["props"]}
    for row in value["characters"] + value["props"]:
        row["key"] = mapping[row["key"]]
    for scene in value["scenes"]:
        for subject in scene["subjects"]:
            subject["character"] = mapping[subject["character"]]
        for prop in scene["props"]:
            prop["prop"] = mapping[prop["prop"]]
            prop["holder"] = mapping.get(prop["holder"])
    renamed = make(StoryDraft.model_validate(value).model_dump(mode="json"))
    profile = project.settings().presets["klein"]
    assert [scene_input(original, scene, profile) for scene in original.scenes] == [
        scene_input(renamed, scene, profile) for scene in renamed.scenes
    ]
    repartitioned = case.draft.model_dump(mode="json")
    for scene in repartitioned["scenes"]:
        scene["source_refs"] = ["p1"]
    merged = make(repartitioned, "".join(case.source.split("\n\n")))
    assert original.request.source_sha256 != merged.request.source_sha256
    assert [scene_input(original, scene, profile) for scene in original.scenes] == [
        scene_input(merged, scene, profile) for scene in merged.scenes
    ]
