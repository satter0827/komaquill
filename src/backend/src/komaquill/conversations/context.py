"""Project canonical state without deriving facts from generated prose."""

from komaquill.conversations.models import Conversation
from komaquill.domain.errors import KomaquillError
from komaquill.domain.roleplay import PersonDefinition, PersonState


def require_reviewed(conversation: Conversation) -> None:
    if any(
        person.visual_review_required
        for person in (conversation.definition.user, conversation.definition.character)
    ):
        raise KomaquillError(code="visual_review_required", retryable=False)


def visible_person(person: PersonDefinition, state: PersonState) -> dict[str, object]:
    if person.visual_review_required:
        raise KomaquillError(code="visual_review_required", retryable=False)
    return {
        "age": person.age,
        "gender": person.gender,
        "physical_features": person.appearance,
        "clothing": person.initial_clothing if state.clothing is None else state.clothing,
        "expression": state.expression,
        "pose": state.pose,
        "held_objects": person.initial_held_objects
        if state.held_objects is None
        else state.held_objects,
    }


def scene_context(conversation: Conversation) -> dict[str, object]:
    return {
        "current_state": conversation.state.model_dump(
            mode="json", exclude={"player": {"appearance"}, "character": {"appearance"}}
        ),
        "visible_scene": drawing_context(conversation),
    }


def drawing_context(conversation: Conversation) -> dict[str, object]:
    """Resolve the visible partner while keeping the player out of the image."""
    require_reviewed(conversation)
    visible = conversation.state.character_place == conversation.state.place
    return {
        "character": visible_person(conversation.definition.character, conversation.state.character)
        if visible
        else None,
        "character_visible": visible,
        "setting": {
            "place": conversation.state.place,
            "time": conversation.state.time,
            "elapsed_minutes": conversation.state.elapsed_minutes,
            "scenery": conversation.state.scenery,
        },
    }
