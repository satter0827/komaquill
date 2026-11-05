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
from typing import Any
from uuid import uuid5

from pydantic import BaseModel, ConfigDict, Field

from tools.reporting import atomic_json, message

ROOT = Path(__file__).resolve().parents[3]


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
    """Build history through public operations and preserve all rejected results."""
    sys.path.insert(0, str(source))
    import komaquill
    from komaquill.api import Client, ConversationInput, SendInput

    if not Path(str(komaquill.__file__)).resolve().is_relative_to(source.resolve()):
        raise RuntimeError(message("efficiency.source_mismatch"))
    limits = Limits.model_validate(
        tomllib.loads(Path(__file__).with_name("budget.toml").read_text(encoding="utf-8"))
    )
    cases = json.loads((output / "fixtures.json").read_bytes())
    results = []
    calls = []
    for case in cases:
        locale = case["locale"]
        workspace = output / variant / f"{locale}-{'long' if case['long'] else 'short'}"
        api = Client.initialize(workspace)
        (workspace / "komaquill.toml").write_text(
            f"[conversation]\ncontext_window_tokens={limits.context_window_tokens}\n"
            f"max_output_tokens={limits.max_output_tokens}\nrequest_timeout={limits.request_timeout}\n",
            encoding="utf-8",
        )
        api.limit_llm_calls(
            output / "allowance.sqlite3", max_calls=limits.max_calls, max_seconds=limits.max_seconds
        )
        conversation = api.conversations.create(
            ConversationInput.model_validate(
                {
                    "title": "Comparison",
                    "locale": locale,
                    "user": {"name": "Akari", "age": "28"},
                    "character": {"name": "Ren", "age": "60", "role": "bookshop owner"},
                    "scenario": {"place": "Bookshop", "world": "A contemporary harbor town"},
                }
            )
        )
        # Paired reference replies are evaluation data, never inserted as model output.
        inputs = [pair[0] for pair in case["history"]] + case["inputs"]
        for index, text in enumerate(inputs):
            if index == len(inputs) - 1:
                api.close()
                api = Client(workspace)
            operation = api.conversations.send(
                conversation.id,
                SendInput(
                    operation_id=uuid5(conversation.id, str(index)),
                    expected_revision=api.conversations.overview(conversation.id).revision,
                    text=text,
                    locale=locale,
                    allow_real_llm=True,
                ),
            )
            calls.extend(api.llm_records(operation.id))
            saved = api.conversations.get(conversation.id)
            results.append(
                {
                    "locale": locale,
                    "long": case["long"],
                    "index": index,
                    "operation": operation.model_dump(mode="json"),
                    "conversation": saved.model_dump(mode="json"),
                    "usage": api.operation_usage(operation.id),
                }
            )
            atomic_json(
                output / f"{variant}.json",
                {
                    "format_version": 1,
                    "results": results,
                    "calls": calls,
                    "budget": api.llm_usage(),
                    "quality": "not_evaluated",
                },
            )
            if operation.state != "complete":
                break
        api.close()


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
        tomllib.loads(Path(__file__).with_name("budget.toml").read_text(encoding="utf-8"))
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
                "scripts.evaluation.llm.compare",
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
