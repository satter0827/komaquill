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


@pytest.mark.parametrize("variant", ["color", "owner", "left to right", "right to left"])
def test_changed_source_conditions_reach_the_common_compiler(project, variant):
    from komaquill.application.stories.image_inputs import scene_input

    case = cases()["flying-hat" if " to " in variant else "red-book"]
    value = case.draft.model_dump(mode="json")
    source = case.source
    if variant == "color":
        source = source.replace("赤い本", "緑の本").replace("青い鞄", "黄色い鞄")
        value = json.loads(
            json.dumps(value, ensure_ascii=False)
            .replace("赤い本", "緑の本")
            .replace("青い鞄", "黄色い鞄")
            .replace("one red hardcover book", "one green hardcover book")
            .replace("one blue shoulder bag", "one yellow shoulder bag")
        )
    elif variant == "owner":

        def swap_names(text):
            return (
                text.replace("亜紀", "__actor__").replace("蓮", "亜紀").replace("__actor__", "蓮")
            )

        source = swap_names(source)
        mapping = {"aki": "ren", "ren": "aki", None: None}
        for scene in value["scenes"]:
            scene["narrative"] = swap_names(scene["narrative"])
            for subject in scene["subjects"]:
                subject["character"] = mapping[subject["character"]]
                subject["action"] = (
                    subject["action"]
                    .replace("Aki", "__actor__")
                    .replace("Ren", "Aki")
                    .replace("__actor__", "Ren")
                )
            for prop in scene["props"]:
                prop["holder"] = mapping[prop["holder"]]
    else:
        direction = "左から右へ" if variant == "left to right" else "右から左へ"
        source = source.replace("道を転がり", "道を" + direction + "転がり")
        value["scenes"][2]["narrative"] = value["scenes"][2]["narrative"].replace(
            "道を転がり", "道を" + direction + "転がり"
        )
        value["scenes"][2]["props"][0]["state"] = (
            "rolling from " + variant + " on dry ground before the puddle"
        )
    options = settings(project, images=4, llm_profile="fake")
    base = planner(project, options, allow_real_llm=False)
    plan = plan_story(
        FileStoryRepository(project),
        LangChainPlanner(base.provenance, FakeListLLM(responses=[json.dumps(value)], cache=False)),
        request(4, source),
        max_images=8,
    )
    profile = project.settings().presets["klein"]
    image = scene_input(plan, plan.scenes[2 if " to " in variant else 0], profile)
    if variant == "color":
        assert "green hardcover book" in image.positive and "yellow shoulder bag" in image.positive
        assert (
            "red hardcover book" not in image.positive and "blue shoulder bag" not in image.positive
        )
    elif variant == "owner":
        assert (
            "adult man" in image.subject_prompts[0] and "hardcover book" in image.subject_prompts[0]
        )
        assert (
            "adult woman" in image.subject_prompts[1] and "shoulder bag" in image.subject_prompts[1]
        )
        assert "shoulder bag" not in image.subject_prompts[0]
        assert "hardcover book" not in image.subject_prompts[1]
    else:
        assert variant in image.positive and not image.subject_prompts
        opposite = "right to left" if variant == "left to right" else "left to right"
        assert opposite not in image.positive
