"""Collect one bounded comparison without invoking external evaluators or GPUs."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field

from scripts._support import atomic_json, message

ROOT = Path(__file__).resolve().parents[1]


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_calls: int = Field(ge=1)
    max_seconds: int = Field(ge=1)
    history_turns: int = Field(ge=3)
    context_window_tokens: int = Field(ge=1024)
    max_output_tokens: int = Field(ge=128)
    request_timeout: float = Field(gt=0)


def source_hashes(source: Path) -> dict[str, str]:
    return {
        p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(source.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def reserve(path: Path) -> int:
    """Persist the call allowance before sending; interrupted calls remain counted."""
    with FileLock(str(path.with_suffix(".lock"))):
        value = json.loads(path.read_bytes())
        if len(value["calls"]) >= value["max_calls"] or time.time() >= value["deadline"]:
            raise RuntimeError(message("efficiency.budget_exhausted"))
        index = len(value["calls"])
        value["calls"].append({"status": "accepted"})
        atomic_json(path, value)
        return index


def finish(path: Path, index: int, value: dict[str, Any]) -> None:
    with FileLock(str(path.with_suffix(".lock"))):
        record = json.loads(path.read_bytes())
        record["calls"][index].update(value)
        atomic_json(path, record)


def fixture(locale: str, long: bool, count: int) -> dict[str, Any]:
    japanese = locale == "ja"
    return {
        "locale": locale,
        "long": long,
        "history": [
            ("明日の合言葉は銀のコンパスにしましょう。", "明日の合言葉は銀のコンパスですね。")
            if japanese
            else (
                "Let tomorrow's password be silver compass.",
                "Tomorrow's password is silver compass.",
            ),
            ("私は古い切符を青い箱にしまいました。", "あなたの切符は青い箱の中ですね。")
            if japanese
            else ("I put my old ticket in the blue box.", "Your ticket is in the blue box."),
            *[
                ("窓の外の雨が静かですね。", "ええ、店の中は暖かいですね。")
                if japanese
                else ("The rain outside is quiet.", "Yes, the shop is warm.")
                for _ in range(count - 2)
            ],
        ]
        if long
        else [],
        "inputs": (
            [
                "明日の合言葉は何でしたか？",
                "訂正します。私は切符を緑の箱に移しました。",
                "箱の話は置いて、一緒に海へ行きましょう。",
                "合言葉と切符をしまった箱を教えてください。",
            ]
            if japanese
            else [
                "What was tomorrow's password?",
                "Correction: I moved my ticket to the green box.",
                "Let's leave the boxes aside and go to the beach together now.",
                "Tell me the password and which box holds my ticket.",
            ]
        )
        if long
        else (
            [
                "一緒に海へ行きましょう。今すぐ出発します。",
                "私は赤いコートを脱ぎます。あなたが持っていてください。",
            ]
            if japanese
            else [
                "Let's go to the beach together. I am leaving now.",
                "I take off my red coat. Please hold it for me.",
            ]
        ),
    }


def child(source: Path, output: Path, variant: str) -> None:
    """Use the selected package in a separate interpreter and private workspaces."""
    sys.path.insert(0, str(source))
    import komaquill
    import langchain_openai
    from komaquill.adapters.database import ConversationRepository, conversations
    from komaquill.api import Client, ConversationInput, SendInput
    from komaquill.conversations.models import Turn
    from langchain_core.messages.utils import count_tokens_approximately

    if not Path(str(komaquill.__file__)).resolve().is_relative_to(source.resolve()):
        raise RuntimeError(message("efficiency.source_mismatch"))
    from sqlalchemy import update

    budget = output / "budget.json"
    invoke = langchain_openai.ChatOpenAI.invoke

    def bounded(model: Any, messages: Any, *args: Any, **kwargs: Any) -> Any:
        index = reserve(budget)
        started = time.monotonic()
        try:
            response = invoke(model, messages, *args, **kwargs)
        except Exception as error:
            finish(
                budget,
                index,
                {
                    "status": "failed",
                    "variant": variant,
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
                "variant": variant,
                "elapsed_seconds": time.monotonic() - started,
                "usage": response.usage_metadata,
                "response_model": response.response_metadata.get("model_name")
                or response.response_metadata.get("model"),
                "estimated_message_tokens": count_tokens_approximately(
                    messages, chars_per_token=1.0
                ),
            },
        )
        return response

    cast(Any, langchain_openai.ChatOpenAI).invoke = bounded
    cases = json.loads((output / "fixtures.json").read_bytes())
    results = []
    record = output / f"{variant}.json"
    for case in cases:
        locale = case["locale"]
        workspace = output / variant / f"{locale}-{'long' if case['long'] else 'short'}"
        api = Client.initialize(workspace)
        settings = Limits.model_validate(
            tomllib.loads(
                Path(__file__).with_name("llm-efficiency.toml").read_text(encoding="utf-8")
            )
        )
        (workspace / "komaquill.toml").write_text(
            f"[conversation]\ncontext_window_tokens={settings.context_window_tokens}\n"
            f"max_output_tokens={settings.max_output_tokens}\nrequest_timeout={settings.request_timeout}\n",
            encoding="utf-8",
        )
        definition = ConversationInput.model_validate(
            dict(
                title="Comparison",
                locale=locale,
                user={"name": "Akari", "age": "28", "gender": "woman"},
                character={"name": "Ren", "age": "60", "gender": "man", "role": "bookshop owner"},
                scenario={"world": "A contemporary seaside town"},
                initial_state={
                    "place": "古書店" if locale == "ja" else "Bookshop",
                    "goal": "一緒に過ごす" if locale == "ja" else "Spend time together",
                    "player": {"clothing": "赤いコート" if locale == "ja" else "red coat"},
                    "character": {"clothing": "黒いエプロン" if locale == "ja" else "black apron"},
                },
            )
        )
        conversation = api.conversations.create(definition)
        history = [
            Turn(
                operation_id=uuid5(NAMESPACE_URL, f"comparison-{locale}-{index}"),
                user=pair[0],
                assistant=pair[1],
                narration="",
                state=conversation.state,
            )
            for index, pair in enumerate(case["history"])
        ]
        conversation = conversation.model_copy(update={"turns": history, "revision": len(history)})
        with cast(
            ConversationRepository, api.conversations.repository
        ).database.engine.begin() as connection:
            repository = cast(ConversationRepository, api.conversations.repository)
            if "turn_count" in type(conversation).model_fields:
                conversation = conversation.model_copy(update={"turn_count": len(history)})
                repository._store(connection, conversation)
            else:
                connection.execute(
                    update(conversations)
                    .where(conversations.c.id == str(conversation.id))
                    .values(payload=conversation.model_dump_json())
                )
        for index, text in enumerate(case["inputs"]):
            if index == len(case["inputs"]) - 1:
                if hasattr(api, "close"):
                    api.close()
                api = Client(workspace)
            operation = api.conversations.send(
                conversation.id,
                SendInput(
                    expected_revision=api.conversations.overview(conversation.id).revision,
                    operation_id=uuid5(conversation.id, str(index)),
                    text=text,
                    locale=locale,
                    allow_real_llm=True,
                ),
            )
            saved = api.conversations.get(conversation.id)
            results.append(
                {
                    "locale": locale,
                    "long": case["long"],
                    "index": index,
                    "input": text,
                    "operation": operation.model_dump(mode="json"),
                    "conversation": saved.model_dump(mode="json"),
                }
            )
            atomic_json(record, {"format_version": 1, "results": results})
            print(
                message(
                    "efficiency.progress",
                    variant=variant,
                    locale=locale,
                    history=case["long"],
                    turn=index,
                    state=operation.state,
                ),
                flush=True,
            )
            if operation.state != "complete":
                break


def main() -> None:
    parser = argparse.ArgumentParser(description=message("efficiency.description"))
    parser.add_argument(
        "--allow-real-llm", action="store_true", required=True, help=message("review.allow_llm")
    )
    parser.add_argument("--baseline", type=Path, required=True, help=message("efficiency.baseline"))
    parser.add_argument("--output", type=Path, required=True, help=message("efficiency.output"))
    parser.add_argument("--child", choices=("before", "after"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".artifacts"):
        parser.error(message("efficiency.output"))
    if args.child:
        child(args.baseline, output, args.child)
        return
    output.mkdir(parents=True, exist_ok=False)
    config = Limits.model_validate(
        tomllib.loads(Path(__file__).with_name("llm-efficiency.toml").read_text(encoding="utf-8"))
    ).model_dump()
    atomic_json(
        output / "budget.json",
        {**config, "deadline": time.time() + config["max_seconds"], "calls": []},
    )
    atomic_json(
        output / "fixtures.json",
        [
            fixture(locale, long, config["history_turns"])
            for locale in ("ja", "en")
            for long in (False, True)
        ],
    )
    sources = {"before": args.baseline.resolve(), "after": ROOT / "src/backend/src"}
    if any(not (source / "komaquill/__init__.py").is_file() for source in sources.values()):
        parser.error(message("efficiency.baseline"))
    manifest = {name: source_hashes(source) for name, source in sources.items()}
    atomic_json(output / "sources.json", manifest)
    for variant, source in sources.items():
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "scripts.compare_llm",
                "--allow-real-llm",
                "--baseline",
                str(source),
                "--output",
                str(output),
                "--child",
                variant,
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            check=False,
            timeout=max(
                1, json.loads((output / "budget.json").read_bytes())["deadline"] - time.time()
            ),
        )
        if result.returncode:
            raise SystemExit(result.returncode)
    unchanged = all(source_hashes(source) == manifest[name] for name, source in sources.items())
    atomic_json(
        output / "completion.json",
        {
            "sources_unchanged": unchanged,
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
    )
    if not unchanged:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
