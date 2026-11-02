"""Prepare saved scene fixtures without exposing an unrestricted product command."""

from komaquill.adapters.database import conversations


def set_state(client, identity, state, expected_revision):
    repository = client.conversations.repository
    current = repository.get(identity)
    assert current.revision == expected_revision
    result = current.model_copy(update={"state": state, "revision": current.revision + 1})
    with repository.database.engine.begin() as connection:
        connection.execute(
            conversations.update()
            .where(conversations.c.id == str(identity))
            .values(payload=result.model_dump_json())
        )
    return result


def wire_scene(state):
    return {
        "relationship": state.relationship,
        "companion_movement": "stay",
        "scenery": state.scenery,
        "character": state.character.model_dump(exclude={"appearance"}),
    }
