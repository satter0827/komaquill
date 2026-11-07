"""Project canonical state without deriving facts from generated prose."""

from komaquill.domain.conversations.models import Conversation
from komaquill.domain.roleplay import PersonDefinition, PersonState, visual_appearance


def visible_person(person: PersonDefinition, state: PersonState) -> dict[str, object]:
    clothing = person.initial_clothing if state.clothing is None else state.clothing
    return {
        "age": person.age,
        "gender": person.gender,
        **visual_appearance(person, clothing, field="physical_features"),
        "clothing": clothing,
        "expression": state.expression,
        "pose": state.pose,
        "held_objects": [
            item.model_dump()
            for item in (
                person.initial_held_objects if state.held_objects is None else state.held_objects
            )
        ],
    }


def scene_context(conversation: Conversation) -> dict[str, object]:
    return {
        "current_state": conversation.state.model_dump(
            mode="json", exclude={"player", "character"}
        ),
        "people": {
            "player": visible_person(conversation.definition.user, conversation.state.player),
            "character": visible_person(
                conversation.definition.character, conversation.state.character
            ),
        },
    }


def drawing_context(
    conversation: Conversation, subjects: str = "character", viewpoint: str = "player"
) -> dict[str, object]:
    """Resolve the visible partner while keeping the player out of the image."""
    visible = subjects != "environment"
    if subjects == "pair":
        return {
            "subjects": {
                "player": visible_person(conversation.definition.user, conversation.state.player),
                "character": visible_person(
                    conversation.definition.character, conversation.state.character
                ),
            },
            "viewpoint": viewpoint,
            "character_visible": True,
            "setting": {
                "place": conversation.state.place,
                "time": conversation.state.time,
                "scenery": conversation.state.scenery,
            },
        }
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
