"""Keep historical source states immutable while exposing one shared current place."""

import json
from uuid import uuid4

from komaquill.api import Client, ConversationInput, MoveAction, SendInput
from komaquill.domain.conversations.editing import NotesRequest, SceneRequest
from komaquill.domain.conversations.history import advance_history
from komaquill.domain.conversations.models import Reply
from sqlalchemy import select


def test_old_states_remain_readable_without_rewriting_history_or_action_evidence(tmp_path):
    from komaquill.adapters.persistence.schema import (
        conversation_events,
        conversation_turns,
        conversations,
    )

    with Client.initialize(tmp_path) as api:
        scene = api.conversations.create(
            ConversationInput(title="Retained", llm_profile="fake", initial_state={"place": "Shop"})
        )
        api.conversations.responder.reply = lambda *_: Reply(text="We are here.", state_patch={})
        request = SendInput(
            operation_id=uuid4(),
            expected_revision=0,
            text="Walk and talk.",
            action=MoveAction(destination="Park"),
        )
        assert api.conversations.send(scene.id, request).state == "complete"
        repository = api.conversations.repository
        with repository.database.engine.begin() as connection:
            current = json.loads(connection.execute(select(conversations.c.payload)).scalar_one())
            current["state"]["character_place"] = "Elsewhere"
            current["definition"]["initial_state"]["character_place"] = "Shop"
            connection.execute(conversations.update().values(payload=json.dumps(current)))
            turn = json.loads(connection.execute(select(conversation_turns.c.payload)).scalar_one())
            turn["state"]["character_place"] = "Elsewhere"
            turn_bytes = json.dumps(turn)
            prefix = advance_history("", turn)
            connection.execute(
                conversation_turns.update().values(payload=turn_bytes, prefix_sha256=prefix)
            )
            event = json.loads(
                connection.execute(select(conversation_events.c.payload)).scalar_one()
            )
            event.pop("origin", None)
            event["before"]["character_place"] = "Shop"
            event["after"]["character_place"] = "Elsewhere"
            event_bytes = json.dumps(event)
            connection.execute(conversation_events.update().values(payload=event_bytes))
        saved = api.conversations.get(scene.id)
        assert saved.state.place == "Park" and saved.turns[0].state.place == "Park"
        assert "character_place" not in saved.state.model_dump()
        assert len(saved.actions) == 1 and saved.actions[0].origin == "explicit"
        assert repository.history_hash(scene.id, 1) == prefix
        api.conversations.save_notes(
            scene.id,
            NotesRequest(
                operation_id=uuid4(), expected_revision=saved.revision, notes=["A retained note"]
            ),
        )
        with repository.database.engine.connect() as connection:
            assert (
                connection.execute(select(conversation_turns.c.payload)).scalar_one() == turn_bytes
            )
            assert (
                connection.execute(select(conversation_turns.c.prefix_sha256)).scalar_one()
                == prefix
            )
            assert (
                connection.execute(select(conversation_events.c.payload)).scalar_one()
                == event_bytes
            )
        updated = api.conversations.get(scene.id)
        fork = api.conversations.fork(
            scene.id,
            SceneRequest(
                operation_id=uuid4(),
                expected_revision=updated.revision,
                turn_id=request.operation_id,
                name="Continue together",
            ),
        )
        assert fork.state.place == "Park" and fork.source_conversation_id == scene.id
        assert api.conversations.get(scene.id).turns[0].user == request.text
