"""Prepare saved scene fixtures without exposing an unrestricted product command."""

from typing import Any


def wire_reply(value: dict[str, Any]) -> dict[str, Any]:
    """Compile a paired fixture's declared scene into the response contract."""
    if "current_state" not in value:
        return value
    if value.get("requested_player_action") is not None:
        return {"response": {"kind": "player", "action": value["requested_player_action"]}}
    scene = value["current_state"]
    changes = []
    if isinstance(scene, dict):
        for key, item in scene.items():
            if key == "character":
                changes.extend(
                    {"field": name, "value": entry}
                    for name, entry in item.items()
                    if entry is not None
                )
            else:
                changes.append(
                    {"field": "movement" if key == "companion_movement" else key, "value": item}
                )
    else:
        changes = scene
    return {
        "response": {
            "kind": "companion",
            "changes": changes,
            **{key: value[key] for key in ("action_narration", "text") if key in value},
        }
    }


def set_state(client, identity, state, expected_revision):
    repository = client.conversations.repository
    current = repository.get(identity)
    assert current.revision == expected_revision
    result = current.model_copy(update={"state": state, "revision": current.revision + 1})
    with repository.database.engine.begin() as connection:
        repository._store(connection, result)
    return result


def wire_scene(state):
    return {
        "relationship": state.relationship,
        "companion_movement": "stay",
        "scenery": state.scenery,
        "character": state.character.model_dump(exclude={"appearance"}),
    }
