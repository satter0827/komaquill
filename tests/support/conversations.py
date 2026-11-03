"""Prepare saved scene fixtures without exposing an unrestricted product command."""


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
