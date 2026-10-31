"""Project saved definitions and scene memory into explicit generation context."""

import json

from komaquill.conversations.models import Conversation

VISUAL_MEMORY_KEYS = frozenset(
    {
        "character_clothing",
        "character_expression",
        "character_pose",
        "character_held_objects",
        "character_location",
        "character_appearance",
        "player_name",
        "player_age",
        "player_gender",
        "player_role",
        "player_appearance",
        "scene",
    }
)


def definition_value(text: str) -> str | dict[str, object]:
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return value if isinstance(value, dict) else text


def scene_context(conversation: Conversation) -> dict[str, object]:
    latest = conversation.turns[-1] if conversation.turns else None
    changes = (
        {
            key: conversation.state.get(key, "")
            for key in conversation.state.keys() | latest.state.keys()
            if conversation.state.get(key) != latest.state.get(key)
        }
        if latest
        else {}
    )
    return {
        "latest_scene": latest.narration if latest else "",
        "state": {
            key: value
            for key, value in conversation.state.items()
            if not (latest and latest.narration and key in VISUAL_MEMORY_KEYS)
        },
        "scene_settings": changes,
    }
