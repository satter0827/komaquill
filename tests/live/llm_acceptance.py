"""Collect explicitly authorized local-model evidence with a shared call budget."""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from scripts._support import atomic_json, message
from scripts.compare_llm import finish, reserve, source_hashes


def instrument(budget: Path, label: str) -> None:
    import langchain_openai

    invoke = langchain_openai.ChatOpenAI.invoke

    def bounded(model: Any, messages: Any, *args: Any, **kwargs: Any) -> Any:
        index = reserve(budget)
        started = time.monotonic()
        try:
            result = invoke(model, messages, *args, **kwargs)
        except Exception as error:
            finish(
                budget,
                index,
                {
                    "status": "failed",
                    "label": label,
                    "exception_type": type(error).__name__,
                    "usage": None,
                },
            )
            raise
        finish(
            budget,
            index,
            {
                "status": "response",
                "label": label,
                "elapsed_seconds": time.monotonic() - started,
                "usage": result.usage_metadata,
                "model": result.response_metadata.get("model_name"),
            },
        )
        return result

    langchain_openai.ChatOpenAI.invoke = bounded  # type: ignore[method-assign]


def diagnose(source: Path, output: Path, *, model: str | None = None) -> None:
    sys.path.insert(0, str(source))
    from komaquill.api import Client, ConversationInput, SendInput
    from komaquill.conversations.history import advance_history
    from komaquill.conversations.models import Turn
    from komaquill.domain.roleplay import ConversationMemory

    output.mkdir(exist_ok=False)
    sources = source_hashes(source)
    results: list[dict[str, Any]] = []
    for locale in ("en", "ja"):
        japanese = locale == "ja"
        question = (
            "先ほど地図に何を描きましたか？"
            if japanese
            else "What did you draw on the paper map earlier?"
        )
        action = (
            "レンは紙の地図に銀色の三角形を描いた。"
            if japanese
            else "Ren draws a silver triangle on the paper map."
        )
        for context in ("history", "verified_memory", "retained_summary"):
            workspace = output / f"{locale}-{context}"
            api = Client.initialize(workspace)
            configuration = (
                "[conversation]\nmax_output_tokens=1536\nrepair_attempts=0\nrequest_timeout=180.0\n"
            )
            if model:
                configuration += f"\n[llm_profiles.lmstudio-4b]\nmodel={json.dumps(model)}\n"
            (workspace / "komaquill.toml").write_text(configuration, encoding="utf-8")
            scene = api.conversations.create(
                ConversationInput(
                    title="Remembered drawing",
                    locale=locale,
                    user={"name": "Kai", "age": "28"},
                    character={"name": "Ren", "age": "40"},
                    initial_state={
                        "place": "Bookshop",
                        "player": {"clothing": "red coat"},
                        "character": {"clothing": "black apron"},
                    },
                )
            )
            turn = Turn(
                operation_id=uuid5(NAMESPACE_URL, f"drawing-{locale}"),
                user="All right.",
                assistant="",
                narration="",
                action_narration=action,
                state=scene.state,
            )
            memory = None
            if context != "history":
                if context == "verified_memory":
                    summary = action
                else:
                    summary = (
                        "レンは地図に何かを描き、その後で地図を折ってカウンターに置いた。"
                        if japanese
                        else "Ren drew on a paper map, then folded it and left it on the counter."
                    )
                memory = ConversationMemory(
                    source_version=5
                    if context == "verified_memory"
                    and "passages" in ConversationMemory.model_fields
                    else 4,
                    text=summary,
                    through_turn=1,
                    source_sha256=advance_history("", turn.model_dump(mode="json")),
                    locale=locale,
                    model="retained-evidence",
                    instructions_sha256="diagnostic",
                    **(
                        {
                            "passages": [
                                {
                                    "turn_id": str(turn.operation_id),
                                    "field": "action_narration",
                                    "quote": action,
                                }
                            ]
                        }
                        if context == "verified_memory"
                        and "passages" in ConversationMemory.model_fields
                        else {}
                    ),
                )
            value = scene.model_copy(
                update={"turns": [turn], "turn_count": 1, "revision": 1, "memory": memory}
            )
            with api.conversations.repository.database.engine.begin() as connection:
                api.conversations.repository._store(connection, value)
            if context == "retained_summary" and "passages" in ConversationMemory.model_fields:
                from sqlalchemy import text

                with api.conversations.repository.database.engine.begin() as connection:
                    connection.execute(text("UPDATE alembic_version SET version_num='0009'"))
                api.close()
                api.conversations.repository.database.engine.dispose()
                api.migrate()
                api = Client(workspace)
            request = SendInput(
                operation_id=uuid5(scene.id, context), text=question, allow_real_llm=True
            )
            started = time.monotonic()
            try:
                operation = api.conversations.send(scene.id, request)
                saved = api.conversations.get(scene.id)
                result = {
                    "locale": locale,
                    "context": context,
                    "question": question,
                    "expected_fact": action,
                    "operation": operation.model_dump(mode="json"),
                    "conversation": saved.model_dump(mode="json"),
                    "elapsed_seconds": time.monotonic() - started,
                }
                results.append(result)
                atomic_json(
                    output / "results.json",
                    {
                        "started_at": datetime.now(UTC).isoformat(),
                        "results": results,
                        "source_hashes": sources,
                        "source_unchanged": source_hashes(source) == sources,
                    },
                )
                print(
                    json.dumps({"locale": locale, "context": context, "state": operation.state}),
                    flush=True,
                )
            finally:
                api.close()


def scenarios(source: Path, output: Path, model: str, *, include_memory: bool = True) -> None:
    sys.path.insert(0, str(source))
    from komaquill.api import ActionRequest, Client, ConversationInput, PrepareAction, SendInput
    from komaquill.conversations.models import Turn

    output.mkdir(exist_ok=False)
    source_snapshot = source_hashes(source)
    results = []
    for locale in ("en", "ja"):
        japanese = locale == "ja"
        workspace = output / locale
        api = Client.initialize(workspace)
        (workspace / "komaquill.toml").write_text(
            "[conversation]\ncontext_window_tokens=5120\nrecent_turns=6\nmax_output_tokens=1536\nrequest_timeout=180.0\n"
            f"\n[llm_profiles.lmstudio-4b]\nmodel={json.dumps(model)}\n",
            encoding="utf-8",
        )
        scene = api.conversations.create(
            ConversationInput(
                title="Retained evidence",
                locale=locale,
                user={"name": "Haruto", "age": "38"},
                character={"name": "Lea", "age": "64", "gender": "woman"},
                initial_state={
                    "place": "Workshop",
                    "player": {"clothing": "grey vest"},
                    "character": {"clothing": "brown jacket"},
                },
            )
        )
        turns = []
        for index in range(48):
            user = (
                "窓の外で雨が静かに降っています。"
                if japanese
                else "Rain is falling quietly outside the window."
            )
            action = ""
            if index == 0:
                user = (
                    "明日の合言葉は銅のランタンです。"
                    if japanese
                    else "Tomorrow's password is copper lantern."
                )
            elif index == 1:
                user = (
                    "古い切符は青い箱に入っています。"
                    if japanese
                    else "The old ticket is in the blue box."
                )
                action = (
                    "リアは木の看板に紫色の円を描いた。"
                    if japanese
                    else "Lea paints a violet circle on the wooden sign."
                )
            elif index == 2:
                user = (
                    "訂正します。切符は緑の箱に移しました。"
                    if japanese
                    else "Correction: I moved the ticket to the green box."
                )
            turns.append(
                Turn(
                    operation_id=uuid5(scene.id, str(index)),
                    user=user,
                    assistant="暖かい部屋ですね。" if japanese else "The room is warm.",
                    action_narration=action,
                    state=scene.state,
                )
            )
        with api.conversations.repository.database.engine.begin() as connection:
            api.conversations.repository._store(
                connection,
                scene.model_copy(
                    update={"turns": turns, "turn_count": len(turns), "revision": len(turns)}
                ),
            )

        def send(
            client: Any, label: str, text: str, identity: Any = scene.id, locale: str = locale
        ) -> Any:
            started = time.monotonic()
            request = SendInput(operation_id=uuid5(identity, label), text=text, allow_real_llm=True)
            operation = client.conversations.send(identity, request)
            value = client.conversations.get(identity)
            results.append(
                {
                    "locale": locale,
                    "case": label,
                    "input": text,
                    "operation": operation.model_dump(mode="json"),
                    "conversation": value.model_dump(mode="json"),
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
            atomic_json(
                output / "results.json",
                {
                    "results": results,
                    "source_hashes": source_snapshot,
                    "source_unchanged": source_hashes(source) == source_snapshot,
                },
            )
            print(
                json.dumps({"locale": locale, "case": label, "state": operation.state}), flush=True
            )
            return value

        if include_memory:
            send(
                api,
                "memory-password",
                "明日の合言葉は何でしたか？" if japanese else "What was tomorrow's password?",
            )
            send(
                api,
                "memory-drawing",
                "木の看板に何を描きましたか？"
                if japanese
                else "What did you paint on the wooden sign?",
            )
            api.close()
            api = Client(workspace)
            send(
                api,
                "memory-correction",
                "古い切符はどの箱にありますか？"
                if japanese
                else "Which box contains the old ticket?",
            )

        action_scene = api.conversations.create(
            ConversationInput(
                title="Confirmed action",
                locale=locale,
                user={"name": "Haruto", "age": "38"},
                character={"name": "Lea", "age": "64"},
                initial_state={
                    "place": "Workshop",
                    "player": {"clothing": "grey vest"},
                    "character": {"clothing": "brown jacket"},
                },
            )
        )
        value = send(
            api,
            "natural-move",
            "私は今から展望台へ移動します。" if japanese else "I enter the observatory now.",
            action_scene.id,
        )
        pending = [p for p in value.proposals if p.state == "pending"]
        if pending:
            action = api.conversations.act(
                action_scene.id,
                ActionRequest(
                    operation_id=uuid5(action_scene.id, "confirm-move"),
                    expected_revision=value.revision,
                    action=pending[0].action,
                    proposal_id=pending[0].id,
                    allow_real_llm=True,
                ),
            )
            operation = api.operations.get(action.reaction_id)
            results.append(
                {
                    "locale": locale,
                    "case": "confirmed-move",
                    "action": action.model_dump(mode="json"),
                    "operation": operation.model_dump(mode="json"),
                    "conversation": api.conversations.get(action_scene.id).model_dump(mode="json"),
                }
            )
        value = api.conversations.get(action_scene.id)
        prepared = api.conversations.act(
            action_scene.id,
            ActionRequest(
                operation_id=uuid5(action_scene.id, "prepare"),
                expected_revision=value.revision,
                action=PrepareAction(clothing="青いシャツ" if japanese else "blue shirt"),
                allow_real_llm=True,
            ),
        )
        results.append(
            {
                "locale": locale,
                "case": "confirmed-clothing",
                "action": prepared.model_dump(mode="json"),
                "operation": api.operations.get(prepared.reaction_id).model_dump(mode="json"),
                "conversation": api.conversations.get(action_scene.id).model_dump(mode="json"),
            }
        )
        send(
            api,
            "companion-request",
            "あなたは黄色い上着を着て、私のいる場所に来てください。"
            if japanese
            else "Please wear a yellow jacket and join me where I am.",
            action_scene.id,
        )
        api.close()
        api = Client(workspace)
        send(
            api,
            "current-clothing",
            "今、私は何を着ていますか？" if japanese else "What am I wearing now?",
            action_scene.id,
        )
        missing = api.conversations.create(ConversationInput(title="Unknown memory", locale=locale))
        send(
            api,
            "unknown-detail",
            "昨日歩いた通りの名前は何でしたか？"
            if japanese
            else "What was the name of the street I walked along yesterday?",
            missing.id,
        )
        api.close()
    atomic_json(
        output / "results.json",
        {
            "results": results,
            "source_hashes": source_snapshot,
            "source_unchanged": source_hashes(source) == source_snapshot,
        },
    )


def benchmark(source: Path, output: Path, model: str) -> None:
    """Include confirmation reactions, memory, repairs and translation in paired totals."""
    sys.path.insert(0, str(source))
    from komaquill.api import ActionRequest, Client, ConversationInput, SendInput
    from komaquill.conversations.models import Turn

    from scripts.compare_llm import fixture

    output.mkdir(exist_ok=False)
    snapshot = source_hashes(source)
    rows = []
    fixtures = [fixture(locale, long, 14) for locale in ("en", "ja") for long in (False, True)]
    for case in fixtures:
        original = case["inputs"]
        case["inputs"] = [original[0], original[1], original[3]] if case["long"] else original[:1]
    atomic_json(output / "fixtures.json", fixtures)
    for case in fixtures:
        locale = case["locale"]
        workspace = output / f"{locale}-{case['long']}"
        api = Client.initialize(workspace)
        (workspace / "komaquill.toml").write_text(
            "[conversation]\ncontext_window_tokens=32768\nmax_output_tokens=1536\nrequest_timeout=180.0\n"
            f"\n[llm_profiles.lmstudio-4b]\nmodel={json.dumps(model)}\n",
            encoding="utf-8",
        )
        scene = api.conversations.create(
            ConversationInput(
                title="Paired comparison",
                locale=locale,
                user={"name": "Akari", "age": "28", "gender": "woman"},
                character={"name": "Ren", "age": "60", "gender": "man"},
                initial_state={
                    "place": "Bookshop",
                    "player": {"clothing": "red coat"},
                    "character": {"clothing": "black apron"},
                },
            )
        )
        turns = [
            Turn(
                operation_id=uuid5(scene.id, f"history-{i}"),
                user=p[0],
                assistant=p[1],
                state=scene.state,
            )
            for i, p in enumerate(case["history"])
        ]
        with api.conversations.repository.database.engine.begin() as connection:
            api.conversations.repository._store(
                connection,
                scene.model_copy(
                    update={"turns": turns, "turn_count": len(turns), "revision": len(turns)}
                ),
            )
        for index, text in enumerate(case["inputs"]):
            if index == len(case["inputs"]) - 1:
                api.close()
                api = Client(workspace)
            started = time.monotonic()
            operation = api.conversations.send(
                scene.id,
                SendInput(
                    operation_id=uuid5(scene.id, str(index)),
                    text=text,
                    locale=locale,
                    allow_real_llm=True,
                ),
            )
            saved = api.conversations.get(scene.id)
            reactions = []
            pending = [p for p in saved.proposals if p.state == "pending"]
            for proposal in pending:
                action = api.conversations.act(
                    scene.id,
                    ActionRequest(
                        operation_id=uuid5(proposal.id, "confirm"),
                        expected_revision=saved.revision,
                        action=proposal.action,
                        proposal_id=proposal.id,
                        allow_real_llm=True,
                    ),
                )
                reactions.append(
                    {
                        "action": action.model_dump(mode="json"),
                        "operation": api.operations.get(action.reaction_id).model_dump(mode="json"),
                    }
                )
                saved = api.conversations.get(scene.id)
            rows.append(
                {
                    "locale": locale,
                    "long": case["long"],
                    "index": index,
                    "input": text,
                    "operation": operation.model_dump(mode="json"),
                    "reactions": reactions,
                    "conversation": saved.model_dump(mode="json"),
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
            atomic_json(
                output / "results.json",
                {
                    "results": rows,
                    "source_hashes": snapshot,
                    "source_unchanged": source_hashes(source) == snapshot,
                },
            )
            print(
                json.dumps(
                    {
                        "locale": locale,
                        "long": case["long"],
                        "index": index,
                        "state": operation.state,
                    }
                ),
                flush=True,
            )
        if case is fixtures[-1]:
            api.library.image_describer.prepare_image(
                '{"character":{"age":"60","gender":"男性","physical_features":"短い白髪と長いひげ","clothing":"黒いエプロン"},"setting":{"place":"古書店"}}',
                uuid5(scene.id, "translation"),
                "lmstudio-4b",
                True,
                compose=True,
                purpose="conversation_image",
            )
        api.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=message("efficiency.description"))
    parser.add_argument(
        "--allow-real-llm", action="store_true", required=True, help=message("review.allow_llm")
    )
    parser.add_argument("--source", type=Path, required=True, help=message("efficiency.baseline"))
    parser.add_argument(
        "--report-root", type=Path, required=True, help=message("efficiency.output")
    )
    parser.add_argument("--label", required=True)
    parser.add_argument("--model")
    parser.add_argument(
        "--mode", choices=("diagnose", "scenarios", "actions", "benchmark"), default="diagnose"
    )
    args = parser.parse_args()
    root = args.report_root.resolve()
    if not root.is_relative_to(Path.cwd() / ".artifacts"):
        parser.error(message("efficiency.output"))
    instrument(root / "budget.json", args.label)
    if args.mode in {"scenarios", "actions", "benchmark"}:
        if not args.model:
            parser.error(message("acceptance.model_required"))
        if args.mode == "benchmark":
            benchmark(args.source.resolve(), root / args.label, args.model)
            return
        scenarios(
            args.source.resolve(),
            root / args.label,
            args.model,
            include_memory=args.mode == "scenarios",
        )
    else:
        diagnose(args.source.resolve(), root / args.label, model=args.model)


if __name__ == "__main__":
    main()
