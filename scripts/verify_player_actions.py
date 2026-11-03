"""Collect bounded real-model evidence for confirmed player actions and memory."""

import argparse
import json
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import patch
from uuid import UUID, uuid4

from scripts._support import atomic_json, message

if TYPE_CHECKING:
    from komaquill.api import Client
    from komaquill.conversations.models import Conversation
from scripts.compare_llm import finish, reserve, source_hashes

ROOT = Path(__file__).resolve().parents[1]


def send_step(
    client: "Client",
    identity: UUID,
    text: str,
    *,
    locale: Literal["ja", "en"],
    events: list[dict[str, Any]],
) -> "Conversation":
    from komaquill.api import SendInput

    operation = client.conversations.send(
        identity, SendInput(operation_id=uuid4(), text=text, locale=locale, allow_real_llm=True)
    )
    events.append({"kind": "dialogue", "operation": operation.model_dump(mode="json")})
    return client.conversations.get(identity)


def collect(client: "Client", output: Path) -> list[dict[str, Any]]:
    from komaquill.api import ActionRequest, ConversationInput, MoveAction, PrepareAction

    results: list[dict[str, Any]] = []
    locales: tuple[Literal["ja", "en"], ...] = ("ja", "en")
    for locale in locales:
        ja = locale == "ja"
        place = "古書店" if ja else "Bookshop"
        conversation = client.conversations.create(
            ConversationInput.model_validate(
                dict(
                    title=f"Action acceptance {locale}",
                    locale=locale,
                    user={
                        "name": "Akari",
                        "age": "28",
                        "gender": "woman",
                        "appearance": "Brown hair",
                        "initial_clothing": "Red coat",
                    },
                    character={
                        "name": "Mira",
                        "age": "32",
                        "gender": "woman",
                        "appearance": "Short silver hair",
                        "initial_clothing": "Black apron",
                    },
                    scenario={
                        "world": "A contemporary harbor town",
                        "place": place,
                        "goal": "Spend time together",
                    },
                )
            )
        )
        events: list[dict[str, Any]] = []

        send = partial(send_step, client, conversation.id, locale=locale, events=events)

        current = send(
            "合言葉は銀のコンパスです。私は今から海へ向かいます。"
            if ja
            else "The password is silver compass. I head to the beach now."
        )
        proposals = [
            p
            for p in current.proposals
            if p.state == "pending" and isinstance(p.action, MoveAction)
        ]
        before_place = current.state.place
        proposed = proposals[0] if proposals else None
        move = proposed.action if proposed else MoveAction(destination="海辺" if ja else "Beach")
        record = client.conversations.act(
            conversation.id,
            ActionRequest(
                operation_id=uuid4(),
                expected_revision=current.revision,
                action=move,
                proposal_id=proposed.id if proposed else None,
                locale=locale,
                allow_real_llm=True,
            ),
        )
        events.append(
            {
                "kind": "confirmed_movement",
                "record": record.model_dump(mode="json"),
                "proposal_found": proposed is not None,
            }
        )
        current = client.conversations.get(conversation.id)
        prepared = client.conversations.act(
            conversation.id,
            ActionRequest(
                operation_id=uuid4(),
                expected_revision=current.revision,
                action=PrepareAction(
                    clothing="青いシャツ" if ja else "Blue shirt",
                    pose="座っている" if ja else "Sitting",
                ),
                locale=locale,
                allow_real_llm=True,
            ),
        )
        events.append({"kind": "confirmed_preparation", "record": prepared.model_dump(mode="json")})
        send(
            "あなたは緑のコートを着て、私のいる場所に来て、椅子に座ってください。"
            if ja
            else "Please wear a green coat, join me where I am, and sit on a chair."
        )
        # Recreate the client to exercise stored summaries and actions rather than in-memory state.
        from komaquill.api import Client

        client = Client(client.workspace)
        send = partial(send_step, client, conversation.id, locale=locale, events=events)
        final = send(
            "合言葉は何でしたか？ 今の場所と、私とあなたが着ている服も教えてください。"
            if ja
            else "What was the password? Tell me where we are and what each of us is wearing."
        )
        result = {
            "locale": locale,
            "events": events,
            "conversation": final.model_dump(mode="json"),
            "checks": {
                "proposal_offered": proposed is not None,
                "unconfirmed_player_did_not_move": before_place == place,
                "movement_applied_once": len(final.actions) == 2,
                "companion_not_moved_by_player_action": record.after.character_place == place,
                "player_clothing_preserved": final.state.player.clothing
                == prepared.after.player.clothing,
                "history_available_after_restart": len(final.turns) == 5,
            },
        }
        atomic_json(output / f"{locale}.json", result)
        results.append(result)
    return results


@contextmanager
def bounded_calls(budget: Path) -> Iterator[None]:
    import langchain_openai

    original = langchain_openai.ChatOpenAI.invoke

    def bounded(model: Any, *values: Any, **options: Any) -> Any:
        index = reserve(budget)
        try:
            response = original(model, *values, **options)
        except Exception as error:
            finish(budget, index, {"status": "failed", "exception_type": type(error).__name__})
            raise
        finish(budget, index, {"status": "response", "usage": response.usage_metadata})
        return response

    with patch.object(langchain_openai.ChatOpenAI, "invoke", bounded):
        yield


def main() -> None:
    parser = argparse.ArgumentParser(description=message("actions.collect_help"))
    parser.add_argument("--allow-real-llm", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / ".artifacts").resolve()):
        parser.error(message("actions.output_path"))
    output.mkdir(parents=True, exist_ok=True)
    limits = tomllib.loads(Path(__file__).with_suffix(".toml").read_text())
    budget = output / "budget.json"
    if not budget.exists():
        atomic_json(
            budget,
            {
                "max_calls": limits["max_calls"],
                "deadline": time.time() + limits["max_seconds"],
                "calls": [],
            },
        )
    source = ROOT / "src/backend/src/komaquill"
    attempt = output / str(uuid4())
    attempt.mkdir()
    before = source_hashes(source)
    atomic_json(attempt / "source.json", {"files": before})
    from komaquill.api import Client

    workspace = attempt / "workspace"
    client = Client.initialize(workspace)
    settings = workspace / "komaquill.toml"
    settings.write_text(
        'models=["wai-illustrious"]\n[conversation]\nrecent_turns=4\nmax_output_tokens=1024\n',
        encoding="utf-8",
    )
    try:
        with bounded_calls(budget):
            collect(client, attempt)
    finally:
        atomic_json(
            attempt / "completion.json",
            {
                "files": source_hashes(source),
                "sources_unchanged": before == source_hashes(source),
                "budget": json.loads(budget.read_bytes()),
            },
        )


if __name__ == "__main__":
    main()
