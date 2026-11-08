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
LONG_CASES = ROOT / "scripts/acceptance/conversations/long.toml"


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_calls: int = Field(ge=1)
    max_seconds: int = Field(ge=1)
    history_turns: int = Field(ge=3)
    context_window_tokens: int = Field(ge=1024)
    max_output_tokens: int = Field(ge=128)
    request_timeout: float = Field(gt=0)


def limits_for(suite: str) -> Limits:
    values = tomllib.loads(Path(__file__).with_name("budget.toml").read_text(encoding="utf-8"))
    return Limits.model_validate({**values["common"], **values[suite]})


def source_hashes(source: Path) -> dict[str, str]:
    return {
        p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(source.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def fixture(locale: str, long: bool, count: int) -> dict[str, Any]:
    japanese = locale == "ja"
    return {
        "id": f"{locale}-{'long' if long else 'short'}",
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
                "こんにちは。あなたの名前を教えてください。",
                "私は赤いコートを着て立ちます。",
            ]
            if japanese
            else [
                "Hello. What is your name?",
                "I put on a red coat and stand up.",
            ]
        ),
        **(
            {"actions": {"1": {"kind": "prepare", "clothing": "Red coat", "pose": "Standing"}}}
            if not long
            else {}
        ),
    }


def fixtures(suite: str, limits: Limits) -> list[dict[str, Any]]:
    cases = [fixture(locale, False, limits.history_turns) for locale in ("ja", "en")]
    if suite == "short":
        return cases
    scenarios = tomllib.loads(LONG_CASES.read_text(encoding="utf-8"))
    cases.append(fixture("en", True, limits.history_turns))
    for identity, scenario in (
        ("ja-long", scenarios),
        ("meeting-ja", scenarios["cases"]["meeting-ja"]),
        ("meeting-en", scenarios["cases"]["meeting-en"]),
    ):
        cases.append(
            {
                "id": identity,
                "locale": scenario.get("locale", "ja"),
                "long": identity == "ja-long",
                "history": [],
                "inputs": scenario["messages"],
                "actions": {
                    index: {"kind": "move", "destination": destination}
                    for index, destination in scenario.get("moves", {}).items()
                },
                "expectations": {
                    name: scenario[name]
                    for name in (
                        "expected_places",
                        "expected_moves",
                        "expected_meetings",
                        "expected_replies",
                    )
                    if name in scenario
                },
            }
        )
    return cases


def child(source: Path, output: Path, variant: str) -> None:
    """Build history through public operations and preserve all rejected results."""
    sys.path.insert(0, str(source))
    import komaquill
    from komaquill.api import Client, ConversationInput, SendInput

    if not Path(str(komaquill.__file__)).resolve().is_relative_to(source.resolve()):
        raise RuntimeError(message("efficiency.source_mismatch"))
    budget = json.loads((output / "budget.json").read_bytes())
    limits = Limits.model_validate({name: budget[name] for name in Limits.model_fields})
    cases = json.loads((output / "fixtures.json").read_bytes())
    results = []
    calls = []
    for case in cases:
        locale = case["locale"]
        workspace = output / variant / case["id"]
        Client.initialize(workspace).close()
        (workspace / "komaquill.toml").write_text(
            f"[conversation]\ncontext_window_tokens={limits.context_window_tokens}\n"
            f"max_output_tokens={limits.max_output_tokens}\nrequest_timeout={limits.request_timeout}\n",
            encoding="utf-8",
        )
        api = Client(workspace)
        api.limit_llm_calls(
            output / "allowance.sqlite3",
            max_calls=limits.max_calls,
            max_seconds=limits.max_seconds,
            create=True,
        )
        japanese_long = case["id"] == "ja-long"
        conversation = api.conversations.create(
            ConversationInput.model_validate(
                {
                    "title": "Comparison",
                    "locale": locale,
                    "user": {"name": "あかり" if japanese_long else "Akari", "age": "28"},
                    "character": {
                        "name": "栞" if japanese_long else "Ren",
                        "age": "32" if japanese_long else "60",
                        "role": "古書店員" if japanese_long else "bookshop owner",
                    },
                    "scenario": {
                        "place": "古書店" if japanese_long else "Bookshop",
                        "world": "現代の港町" if japanese_long else "A contemporary harbor town",
                        "goal": "一緒に読書会を準備する" if japanese_long else "",
                    },
                }
            )
        )
        # Paired reference replies are evaluation data, never inserted as model output.
        inputs = [pair[0] for pair in case["history"]] + case["inputs"]
        for index, text in enumerate(inputs):
            if index == len(inputs) - 1:
                api.close()
                api = Client(workspace)
            request = SendInput.model_validate(
                {
                    "operation_id": uuid5(conversation.id, str(index)),
                    "expected_revision": api.conversations.overview(conversation.id).revision,
                    "text": text,
                    "locale": locale,
                    "allow_real_llm": True,
                    "action": case.get("actions", {}).get(str(index)),
                }
            )
            operation = api.conversations.send(conversation.id, request)
            calls.extend(api.llm_records(operation.id))
            saved = api.conversations.get(conversation.id)
            row = {
                "case": case["id"],
                "locale": locale,
                "long": case["long"],
                "index": index,
                "operation": operation.model_dump(mode="json"),
                "conversation": saved.model_dump(mode="json"),
                "usage": api.operation_usage(operation.id),
            }
            if index == len(inputs) - 1 and operation.state == "complete":
                reserved = api.llm_usage()["reserved_calls"]
                api.close()
                api = Client(workspace)
                retained = api.operations.get(operation.id)
                row["reopened_without_resend"] = (
                    retained.state == "complete"
                    and api.conversations.send(conversation.id, request) == retained
                    and api.llm_usage()["reserved_calls"] == reserved
                    and api.conversations.get(conversation.id) == saved
                )
            results.append(row)
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
            print(
                message(
                    "efficiency.progress",
                    variant=variant,
                    locale=case["id"],
                    history=case["long"],
                    turn=index + 1,
                    state=operation.state,
                ),
                flush=True,
            )
            if operation.state != "complete":
                break
        api.close()


def summarize(report: dict[str, Any], case_count: int, expected_turns: int) -> dict[str, Any]:
    rows = report["results"]
    complete = sum(row["operation"]["state"] == "complete" for row in rows)
    reopened = sum(row.get("reopened_without_resend", False) for row in rows)
    usages = [row["usage"] for row in rows]
    unknown = sum(usage["unknown_calls"] for usage in usages)
    known = [usage["known_usage"] or {} for usage in usages]
    return {
        "complete": complete == len(rows) == expected_turns and reopened == case_count,
        "completed_turns": complete,
        "reopened_cases": reopened,
        "expected_turns": expected_turns,
        "unknown_calls": unknown,
        "calls": sum(usage["call_count"] for usage in usages),
        "known_input_tokens": sum(usage.get("input_tokens", 0) for usage in known),
        "known_output_tokens": sum(usage.get("output_tokens", 0) for usage in known),
        "total_tokens": sum(usage.get("total_tokens", 0) for usage in known)
        if not unknown
        else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=message("efficiency.description"))
    parser.add_argument(
        "--allow-real-llm", action="store_true", required=True, help=message("review.allow_llm")
    )
    parser.add_argument("--baseline", type=Path, required=True, help=message("efficiency.baseline"))
    parser.add_argument(
        "--candidate",
        type=Path,
        default=ROOT / "src/backend/src",
        help=message("efficiency.candidate"),
    )
    parser.add_argument("--output", type=Path, required=True, help=message("efficiency.output"))
    parser.add_argument(
        "--suite", choices=("short", "full"), default="full", help=message("efficiency.suite")
    )
    parser.add_argument("--child", choices=("before", "after"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".artifacts"):
        parser.error(message("efficiency.output"))
    if args.child:
        child(args.baseline, output, args.child)
        return
    limits = limits_for(args.suite)
    cases = fixtures(args.suite, limits)
    minimum_calls = 2 * sum(len(case["history"]) + len(case["inputs"]) for case in cases)
    if minimum_calls > limits.max_calls:
        parser.error(message("efficiency.budget_exhausted"))
    sources = {"before": args.baseline.resolve(), "after": args.candidate.resolve()}
    if any(not (source / "komaquill/__init__.py").is_file() for source in sources.values()):
        parser.error(message("efficiency.baseline"))
    output.mkdir(parents=True, exist_ok=False)
    config = limits.model_dump()
    deadline = time.time() + limits.max_seconds
    atomic_json(
        output / "budget.json",
        {
            **config,
            "suite": args.suite,
            "minimum_calls": minimum_calls,
            "deadline": deadline,
        },
    )
    atomic_json(
        output / "fixtures.json",
        cases,
    )
    manifest = {name: source_hashes(source) for name, source in sources.items()}
    atomic_json(output / "sources.json", manifest)
    runner_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    runs = {}
    timed_out = False
    for variant, source in sources.items():
        remaining = deadline - time.time()
        exit_code = None
        if timed_out or remaining <= 0:
            status = "not_started"
        else:
            try:
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
                    timeout=remaining,
                )
                exit_code = result.returncode
                status = "exited"
            except subprocess.TimeoutExpired:
                timed_out = True
                status = "timed_out"
        report = output / f"{variant}.json"
        runs[variant] = {
            "status": status,
            "exit_code": exit_code,
            **(
                summarize(json.loads(report.read_bytes()), len(cases), minimum_calls // 2)
                if report.is_file()
                else {"complete": False}
            ),
        }
    unchanged = (
        all(source_hashes(source) == manifest[name] for name, source in sources.items())
        and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == runner_hash
    )
    comparable = unchanged and all(
        run["exit_code"] == 0 and run["complete"] and run["unknown_calls"] == 0
        for run in runs.values()
    )
    atomic_json(
        output / "completion.json",
        {
            "sources_unchanged": unchanged,
            "runner_sha256": runner_hash,
            "comparable": comparable,
            "variants": runs,
        },
    )
    if not comparable:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
