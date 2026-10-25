"""Translate visible story intent into portable image-generation inputs."""

from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import CompiledImageInput, compile_image_input, joined
from komaquill.domain.presets import GenerationPreset
from komaquill.domain.stories import Scene, StoryPlan


def scene_input(plan: StoryPlan, scene: Scene, profile: GenerationPreset) -> CompiledImageInput:
    if plan.format_version != 2 or scene.subjects is None:
        raise KomaquillError(
            code="story_replan_required",
            stage="story_rendering",
            retryable=False,
            details={"reason": "replan_required"},
        )
    characters = {character.key: character for character in plan.characters}
    props = {prop.key: prop for prop in plan.props}
    subjects = tuple(
        joined(
            str(characters[subject.character].kind),
            characters[subject.character].appearance,
            subject.action,
            *(
                joined(props[state.prop].appearance, state.state)
                for state in scene.props
                if state.holder == subject.character
            ),
        )
        for subject in scene.subjects
    )
    prompt = joined(
        scene.visual_description,
        scene.location or "",
        str(plan.planner.planning_policy.get("style", "")),
        *(
            joined(props[state.prop].appearance, state.state)
            for state in scene.props
            if state.holder is None
        ),
    )
    return compile_image_input(profile, prompt, subjects=subjects)
