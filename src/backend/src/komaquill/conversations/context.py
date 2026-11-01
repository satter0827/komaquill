"""Project canonical state without deriving facts from generated prose."""

from komaquill.conversations.models import Conversation


def scene_context(conversation: Conversation) -> dict[str, object]:
    return {"current_state": conversation.state.model_dump(mode="json")}


def drawing_context(conversation: Conversation) -> dict[str, object]:
    """Resolve the visible partner while keeping the player out of the image."""
    return {
        "character": conversation.definition.character.model_dump(mode="json"),
        "current_character": conversation.state.character.model_dump(mode="json"),
        "setting": {
            "world": conversation.definition.scenario.world,
            "place": conversation.state.place,
            "time": conversation.state.time,
        },
    }
