"""Prepare and inspect a received response using the isolated loopback fixture."""

import json
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from komaquill.api import Client, ConversationInput, KomaquillError, MoveAction, SendInput


def main() -> None:
    with Client(Path(sys.argv[1])) as api:
        if len(sys.argv) == 2:
            endpoint = api._execution.settings.llm_profiles["lmstudio-4b"].base_url
            assert urlsplit(endpoint).hostname in {"127.0.0.1", "localhost", "::1"}
            scene = api.conversations.create(
                ConversationInput(
                    title="Interrupted move", locale="en", initial_state={"place": "Shop"}
                )
            )
            request = SendInput(
                operation_id=uuid4(),
                expected_revision=0,
                text="Continue.",
                action=MoveAction(destination="Garden"),
                allow_real_llm=True,
            )
            with patch(
                "komaquill.adapters.llm.conversation.check_language",
                side_effect=KomaquillError(code="storage_failed", stage="storage"),
            ):
                operation = api.conversations.send(scene.id, request)
            assert operation.state == "interrupted" and operation.can_resume
        else:
            operation = api.operations.get(UUID(sys.argv[2]))
        saved = api.conversations.get(operation.conversation_id)
        print(
            json.dumps(
                dict(
                    operation_id=str(operation.id),
                    conversation_id=str(saved.id),
                    resume_count=operation.resume_count,
                    turn_count=saved.turn_count,
                    place=saved.state.place,
                    action_count=len(saved.actions),
                    invocation_ids=[r["invocation_id"] for r in api.llm_records(operation.id)],
                )
            )
        )


if __name__ == "__main__":
    main()
