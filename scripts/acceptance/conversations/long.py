"""Run an explicitly permitted long conversation through the installed browser UI."""

import argparse
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from komaquill.api import Client, ConversationInput, KomaquillError, SceneRequest, describe_failure

from scripts.acceptance.run import attach, browse
from tools.observations import owner
from tools.reporting import ROOT, atomic_json, input_fingerprint, message, source_revision

LIMITS = tomllib.loads(Path(__file__).with_suffix(".toml").read_text(encoding="utf-8"))


def collect(
    output: Path,
    *,
    new_conversation: bool = False,
    fork_after_turn: int | None = None,
    through_turn: int | None = None,
    ledger: Path | None = None,
    max_calls: int | None = None,
    max_seconds: int | None = None,
    llm_profile: str | None = None,
) -> bool:
    output = output.resolve()
    if not output.is_relative_to(ROOT / ".artifacts"):
        raise ValueError(message("actions.output_path"))
    output.mkdir(parents=True, exist_ok=False)
    workspace = output.parent / "workspace"
    api: Client | None = None
    report: dict[str, Any] = {
        "format_version": 1,
        **owner(),
        "status": "running",
        "source_revision": source_revision(ROOT),
        "input_sha256": input_fingerprint(ROOT),
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        api = (
            Client(workspace)
            if (workspace / "komaquill.toml").exists()
            else Client.initialize(workspace)
        )
        target = LIMITS["target_turns"] if through_turn is None else through_turn
        if not 1 <= target <= LIMITS["target_turns"]:
            raise KomaquillError(code="invalid_request")
        report["target_turns"] = target
        attach(
            api,
            output,
            ledger=ledger,
            max_calls=LIMITS["max_calls"] if max_calls is None else max_calls,
            max_seconds=LIMITS["max_seconds"] if max_seconds is None else max_seconds,
        )
        case = output.parent / "case.json"
        if new_conversation and fork_after_turn is not None:
            raise KomaquillError(code="invalid_request")
        if fork_after_turn is not None and not case.exists():
            raise KomaquillError(code="invalid_request")
        if case.exists():
            identity = json.loads(case.read_bytes())["conversation_id"]
            if (
                not new_conversation
                and llm_profile is not None
                and api.conversations.overview(UUID(identity)).definition.llm_profile != llm_profile
            ):
                raise KomaquillError(code="invalid_request")
            operations = api.conversations.operations(UUID(identity))
            if any(op.state not in {"complete", "failed"} for op in operations):
                raise KomaquillError(code="operation_busy")
            if (
                not new_conversation
                and fork_after_turn is None
                and any(op.state != "complete" for op in operations)
            ):
                raise KomaquillError(code="operation_busy")
            if fork_after_turn is not None:
                original = api.conversations.get(UUID(identity))
                if not 1 <= fork_after_turn < min(target, original.turn_count + 1):
                    raise KomaquillError(code="invalid_request")
                forked = api.conversations.fork(
                    original.id,
                    SceneRequest(
                        operation_id=uuid4(),
                        expected_revision=original.revision,
                        turn_id=original.turns[fork_after_turn - 1].operation_id,
                        name=original.definition.title,
                    ),
                )
                report["fork"] = {
                    "conversation_id": identity,
                    "through_turn": fork_after_turn,
                    "turn_id": str(original.turns[fork_after_turn - 1].operation_id),
                }
            if new_conversation or fork_after_turn is not None:
                atomic_json(
                    output.parent / "cases" / f"{identity}.json", json.loads(case.read_bytes())
                )
            if fork_after_turn is not None:
                identity = str(forked.id)
                atomic_json(case, {"conversation_id": identity, "fork": report["fork"]})
        if not case.exists() or new_conversation:
            scene = api.conversations.create(
                ConversationInput.model_validate(
                    {
                        "title": "長い会話の検証",
                        "locale": "ja",
                        **({"llm_profile": llm_profile} if llm_profile is not None else {}),
                        "response_length": "standard",
                        "user": {"name": "あかり", "age": "28"},
                        "character": {
                            "name": "栞",
                            "age": "32",
                            "personality": "穏やかで協力的な古書店員",
                        },
                        "scenario": {
                            "place": "古書店",
                            "world": "現代の港町",
                            "goal": "一緒に読書会を準備する",
                        },
                    }
                )
            )
            identity = str(scene.id)
            atomic_json(case, {"conversation_id": identity})
        report["conversation_id"] = identity
        current = api.conversations.overview(UUID(identity))
        report["llm_profile"] = current.definition.llm_profile
        report["starting_turns"] = current.turn_count
        if current.turn_count > target:
            raise KomaquillError(code="invalid_request")
        atomic_json(output / "run.json", report)
        expiry = datetime.fromisoformat(str(api.llm_usage()["expires_at"]))
        remaining = (expiry - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError
        browse(
            api,
            output,
            report,
            {
                "format_version": 1,
                "real_llm": True,
                "conversation": identity,
                "messages": LIMITS["messages"][:target],
                "target_turns": target,
                "expires_at": expiry.isoformat(),
                "moves": LIMITS["moves"],
                "expected_places": LIMITS["expected_places"],
                "expected_character_name": current.definition.character.name,
                "forbidden_held_objects": LIMITS["forbidden_held_objects"],
            },
            script="scripts/acceptance/conversations/long.mjs",
            timeout=remaining,
        )
        report["status"] = "passed"
    except BaseException as error:
        report.update(
            status="failed", failure=describe_failure(error, "operation").model_dump(mode="json")
        )
    finally:
        report["inputs_unchanged"] = input_fingerprint(ROOT) == report["input_sha256"]
        if report["status"] == "passed" and not report["inputs_unchanged"]:
            report["status"] = "inputs_changed"
        if "conversation_id" in report:
            try:
                assert api is not None
                identity = UUID(report["conversation_id"])
                operations = api.conversations.operations(identity)
                report["final_state"] = {
                    "observed_at": datetime.now(UTC).isoformat(),
                    "saved_turns": api.conversations.overview(identity).turn_count,
                    "operations": [
                        {
                            "id": str(operation.id),
                            "state": operation.state,
                            "error": operation.error,
                            "can_resume": operation.can_resume,
                        }
                        for operation in operations
                    ],
                }
            except Exception as error:
                report.update(
                    status="failed",
                    final_state_failure=describe_failure(error, "storage").model_dump(mode="json"),
                )
        if api is not None:
            try:
                report["budget"] = api.llm_usage()
            except Exception as error:
                report.update(
                    status="failed",
                    budget_failure=describe_failure(error, "storage").model_dump(mode="json"),
                )
            try:
                api.close()
            except Exception as error:
                report.update(
                    status="failed",
                    cleanup_failure=describe_failure(error, "operation").model_dump(mode="json"),
                )
        report["finished_at"] = datetime.now(UTC).isoformat()
        atomic_json(output / "run.json", report)
    return report["status"] == "passed" and bool(report["inputs_unchanged"])


def main() -> int:
    parser = argparse.ArgumentParser(description=message("actions.collect_help"))
    parser.add_argument("--allow-real-llm", action="store_true", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-seconds", type=int)
    parser.add_argument("--llm-profile", help=message("acceptance.llm_profile"))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--new-conversation", action="store_true", help=message("acceptance.new_conversation")
    )
    selection.add_argument(
        "--fork-after-turn", type=int, help=message("acceptance.fork_after_turn")
    )
    parser.add_argument("--through-turn", type=int, help=message("acceptance.through_turn"))
    args = parser.parse_args()
    return (
        0
        if collect(
            args.output,
            new_conversation=args.new_conversation,
            fork_after_turn=args.fork_after_turn,
            through_turn=args.through_turn,
            ledger=args.ledger,
            max_calls=args.max_calls,
            max_seconds=args.max_seconds,
            llm_profile=args.llm_profile,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
