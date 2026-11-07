"""Prepare saved scene fixtures without exposing an unrestricted product command."""

from typing import Any


def scripted_llm_availability(monkeypatch):
    """Pair an explicitly substituted model with its read-only availability probe."""
    monkeypatch.setenv(
        "KOMAQUILL_CONVERSATION__LLM_PROFILES__LMSTUDIO-4B__TOKEN_COUNTING", "approximate"
    )
    from komaquill.api.availability import AvailabilityService

    monkeypatch.setattr(AvailabilityService, "_llm", lambda *_: None)


def send_action(service, identity, request):
    """Send a message with an action and inspect its durable action event."""
    service.send(identity, request)
    return next(
        a for a in service.actions(identity) if a.request.operation_id == request.operation_id
    )


def accept_action(service, identity, request):
    """Inspect action persistence before executing the one accepted response."""
    _, created = service.accept(identity, request)
    record = next(
        a for a in service.actions(identity) if a.request.operation_id == request.operation_id
    )
    return record, created


def wire_reply(value: dict[str, Any], *, reaction: bool = False) -> dict[str, Any]:
    """Compile a paired fixture's declared scene into the response contract."""
    if "current_state" not in value:
        return value
    if value.get("requested_player_action") is not None:
        action = value["requested_player_action"]
        if action["kind"] == "move":
            return {
                "player_move": action,
                "response": {
                    "kind": "companion",
                    "meeting_change": None,
                    "meeting_query": None,
                    "changes": [],
                    "action_narration": "",
                    "text": "",
                },
            }
        return {"player_move": None, "response": {"kind": "player", "action": action}}
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
                changes.append({"field": key, "value": item})
    else:
        changes = scene
    return {
        **({} if reaction else {"player_move": None}),
        "response": {
            "kind": "companion",
            "meeting_change": None,
            "meeting_query": None,
            "changes": changes,
            **{
                key: value[key]
                for key in ("action_narration", "text", "meeting_change", "meeting_query")
                if key in value
            },
        },
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
        "scenery": state.scenery,
        "character": state.character.model_dump(exclude={"appearance"}),
    }
