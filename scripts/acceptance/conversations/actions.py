"""Collect bounded real-model evidence for confirmed player actions and memory."""

import argparse
import tomllib
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from tools.observations import owner
from tools.reporting import atomic_json, input_fingerprint, message, source_revision

if TYPE_CHECKING:
    from komaquill.api import Client, Conversation
from scripts.acceptance.run import attach

ROOT = Path(__file__).resolve().parents[3]


def send_step(
    client: "Client",
    identity: UUID,
    text: str,
    *,
    locale: Literal["ja", "en"],
    events: list[dict[str, Any]],
) -> "Conversation":
    from komaquill.api import KomaquillError, SendInput

    operation = client.conversations.send(
        identity,
        SendInput(
            expected_revision=client.conversations.overview(identity).revision,
            operation_id=uuid4(),
            text=text,
            locale=locale,
            allow_real_llm=True,
        ),
    )
    events.append({"kind": "dialogue", "operation": operation.model_dump(mode="json")})
    if operation.state != "complete":
        raise KomaquillError(code=operation.error or "operation_failed")
    return client.conversations.get(identity)


def collect(client: "Client", output: Path) -> list[dict[str, Any]]:
    from komaquill.api import ConversationInput, MoveAction, PrepareAction, SendInput

    with ExitStack() as sessions:
        sessions.callback(client.close)
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

            from komaquill.api import KomaquillError, describe_failure

            result: dict[str, Any] = {"locale": locale, "events": events, "status": "running"}
            try:
                current = send(
                    "合言葉は銀のコンパスです。私は今から海へ向かいます。"
                    if ja
                    else "The password is silver compass. I head to the beach now."
                )
                record = next(
                    a for a in current.actions if isinstance(a.request.action, MoveAction)
                )
                move = record.request.action
                assert isinstance(move, MoveAction)
                movement_completed_with_dialogue = current.turn_count == 1 and bool(
                    current.turns[0].assistant
                )
                events.append(
                    {"kind": "conversation_movement", "record": record.model_dump(mode="json")}
                )
                preparation_operation = client.conversations.send(
                    conversation.id,
                    SendInput(
                        text="Continue.",
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
                if preparation_operation.state != "complete":
                    raise KomaquillError(code=preparation_operation.error or "operation_failed")
                prepared = next(
                    a
                    for a in client.conversations.actions(conversation.id)
                    if a.reaction_id == preparation_operation.id
                )
                events.append(
                    {"kind": "confirmed_preparation", "record": prepared.model_dump(mode="json")}
                )
                send(
                    "あなたは緑のコートを着て、隣の椅子に座ってください。"
                    if ja
                    else "Please wear a green coat and sit in the chair beside me."
                )
                # Reopen storage to exercise persisted summaries and actions.
                from komaquill.api import Client

                workspace = client.workspace
                client.close()
                client = sessions.enter_context(Client(workspace))
                send = partial(send_step, client, conversation.id, locale=locale, events=events)
                final = send(
                    "合言葉は何でしたか？ 今の場所と、私とあなたが着ている服も教えてください。"
                    if ja
                    else (
                        "What was the password? Tell me where we are "
                        "and what each of us is wearing."
                    )
                )
                result = {
                    "locale": locale,
                    "events": events,
                    "conversation": final.model_dump(mode="json"),
                    "checks": {
                        "movement_completed_with_dialogue": movement_completed_with_dialogue,
                        "movement_applied_once": len(final.actions) == 2,
                        "shared_place_after_move": record.after.place == move.destination,
                        "player_clothing_preserved": final.state.player.clothing
                        == prepared.after.player.clothing,
                        "history_available_after_restart": len(final.turns) == 4,
                    },
                }
                result["status"] = "passed" if all(result["checks"].values()) else "failed"
                results.append(result)
            except BaseException as error:
                result.update(
                    status="failed",
                    failure=describe_failure(error, "operation").model_dump(mode="json"),
                )
                raise
            finally:
                atomic_json(output / f"{locale}.json", result)

        return results


def main(argv: list[str] | None = None) -> int:
    from komaquill.api import Client, describe_failure

    parser = argparse.ArgumentParser(description=message("actions.collect_help"))
    parser.add_argument("--allow-real-llm", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-seconds", type=int)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / ".artifacts").resolve()):
        parser.error(message("actions.output_path"))
    output.mkdir(parents=True, exist_ok=True)
    limits = tomllib.loads(Path(__file__).with_name("budget.toml").read_text())
    attempt = output / str(uuid4())
    attempt.mkdir()
    before = input_fingerprint(ROOT)
    report: dict[str, Any] = {
        "format_version": 1,
        **owner(),
        "status": "running",
        "source_revision": source_revision(ROOT),
        "input_sha256": before,
    }
    atomic_json(attempt / "completion.json", report)
    client = None
    try:
        workspace = attempt / "workspace"
        client = Client.initialize(workspace)
        (workspace / "komaquill.toml").write_text(
            'models=["wai-illustrious"]\n[conversation]\nrecent_turns=4\nmax_output_tokens=1024\n',
            encoding="utf-8",
        )
        attach(
            client,
            attempt,
            ledger=args.ledger,
            max_calls=limits["max_calls"] if args.max_calls is None else args.max_calls,
            max_seconds=limits["max_seconds"] if args.max_seconds is None else args.max_seconds,
        )
        results = collect(client, attempt)
        report["status"] = (
            "passed" if results and all(r["status"] == "passed" for r in results) else "failed"
        )
    except BaseException as error:
        report.update(
            status="failed", failure=describe_failure(error, "operation").model_dump(mode="json")
        )
    finally:
        if client is not None:
            try:
                report["budget"] = client.llm_usage()
            except Exception as error:
                report.update(
                    status="failed",
                    budget_failure=describe_failure(error, "storage").model_dump(mode="json"),
                )
            try:
                client.close()
            except Exception as error:
                report.update(
                    status="failed",
                    cleanup_failure=describe_failure(error, "operation").model_dump(mode="json"),
                )
        report["inputs_unchanged"] = input_fingerprint(ROOT) == before
        if report["status"] == "passed" and not report["inputs_unchanged"]:
            report["status"] = "inputs_changed"
        atomic_json(attempt / "completion.json", report)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
