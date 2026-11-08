"""Exercise collection and its budgets with paired fake responses, never a live model."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from scripts.evaluation.llm import compare as comparison
from tests.backend.support.conversations import wire_reply
from tools.reporting import atomic_json

pytestmark = pytest.mark.usefixtures("scripted_services")


def test_budget_counts_incomplete_calls_and_survives_recreation(tmp_path):
    from komaquill.adapters.persistence.budgets import CallBudget
    from komaquill.adapters.persistence.connection import database
    from komaquill.api import Client, KomaquillError

    api = Client.initialize(tmp_path / "workspace")
    ledger = tmp_path / "budget.sqlite3"
    api.limit_llm_calls(ledger, max_calls=1, max_seconds=60, create=True)
    CallBudget(database(ledger), 1, 60).reserve()
    reopened = Client(api.workspace)
    assert reopened.llm_usage()["reserved_calls"] == 1
    with pytest.raises(KomaquillError, match="execution_budget_exceeded"):
        CallBudget(database(ledger), 1, 60).reserve()
    with pytest.raises(KomaquillError, match="operation_conflict"):
        reopened.limit_llm_calls(ledger, max_calls=2, max_seconds=60)


@pytest.mark.parametrize("mode", ["complete", "failed", "interrupted", "unrecorded"])
def test_full_child_uses_explicit_permission_and_saves_actual_failures(tmp_path, monkeypatch, mode):
    import langchain_openai
    from komaquill.adapters.persistence.budgets import CallBudget

    invocations = 0

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **options):
            properties = options["response_format"]["json_schema"]["schema"]["properties"]
            self.summary = "response" not in properties
            self.reaction = not self.summary and "player_move" not in properties
            return self

        def invoke(self, messages, *args, **kwargs):
            nonlocal invocations
            invocations += 1
            if mode == "failed":
                raise RuntimeError("Injected failure")
            if mode == "interrupted":
                if invocations > 1:
                    raise KeyboardInterrupt
                return AIMessage(
                    content="{}",
                    response_metadata={"model_name": self.model},
                    usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
                )
            japanese = "Japanese" in str(messages[0].content) or "language: ja" in str(
                messages[0].content
            )
            text = (
                "明日の合言葉は銀のコンパスです。"
                if japanese
                else "The password is silver compass."
            )
            response = (
                {"text": text}
                if self.summary
                else {
                    "current_state": {
                        "relationship": "",
                        "scenery": "",
                        "character": {
                            "clothing": None,
                            "pose": None,
                            "expression": None,
                            "held_objects": None,
                        },
                    },
                    "action_narration": "",
                    "requested_player_action": None,
                    "text": text,
                }
            )
            return AIMessage(
                content=json.dumps(
                    wire_reply(response, reaction=self.reaction), ensure_ascii=False
                ),
                response_metadata={"model_name": self.model},
                usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    if mode == "unrecorded":
        reserve = CallBudget.reserve

        def reserve_and_interrupt(budget):
            reserve(budget)
            raise KeyboardInterrupt

        monkeypatch.setattr(CallBudget, "reserve", reserve_and_interrupt)
    original_path = list(sys.path)
    output = tmp_path / "output"
    output.mkdir()
    limits = comparison.limits_for("full")
    atomic_json(output / "budget.json", limits.model_dump())
    monkeypatch.setattr(comparison, "limits_for", lambda _: pytest.fail("Use the saved limits"))
    atomic_json(
        output / "fixtures.json",
        [comparison.fixture(locale, long, 14) for locale in ("ja", "en") for long in (False, True)],
    )
    try:
        source = comparison.ROOT / "src/backend/src"
        if mode in {"interrupted", "unrecorded"}:
            with pytest.raises(KeyboardInterrupt):
                comparison.child(source, output, "after")
            comparison.collect_interrupted(source, output, "after")
        else:
            comparison.child(source, output, "after")
    finally:
        sys.path[:] = original_path
    if mode in {"interrupted", "unrecorded"}:
        assert not (output / "after.json").exists()
        evidence = json.loads((output / "after-interrupted.json").read_bytes())
        usage = evidence["usage"]
        assert usage["unknown_calls"] == 1 and not usage["usage_complete"]
        if mode == "interrupted":
            assert invocations == usage["call_count"] == 2
            assert usage["known_usage"]["total_tokens"] == 20
            assert {call["status"] for call in evidence["calls"]} == {
                "response_received",
                "started",
            }
        else:
            assert invocations == 0 and usage["call_count"] == 1
            assert usage["known_usage"] is None and evidence["calls"] == []
        return
    assert not (output / "after-pending.json").exists()
    collected = json.loads((output / "after.json").read_bytes())
    calls = collected["calls"]
    rows = collected["results"]
    failed = mode == "failed"
    assert len(calls) == (4 if failed else 40)
    assert all(item["status"] == ("failed" if failed else "response_received") for item in calls)
    assert len(rows) == (4 if failed else 40)
    assert all(item["operation"]["state"] == ("failed" if failed else "complete") for item in rows)
    if not failed:
        assert all(item["usage"]["total_tokens"] == 20 for item in calls)
        assert rows[-1]["conversation"]["memory"] is None
        assert sum(item.get("reopened_without_resend", False) for item in rows) == 4


@pytest.mark.parametrize("mode", ["complete", "interrupted", "unavailable", "published"])
def test_parent_creates_frozen_inputs_and_uses_separate_interpreters(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(comparison, "ROOT", tmp_path)
    source = tmp_path / "before"
    source.mkdir()
    (source / "sample.py").write_text("pass")
    (source / "komaquill").mkdir()
    (source / "komaquill/__init__.py").write_text("")
    candidate = tmp_path / "src/backend/src"
    candidate.mkdir(parents=True)
    (candidate / "sample.py").write_text("pass")
    (candidate / "komaquill").mkdir()
    (candidate / "komaquill/__init__.py").write_text("")
    output = tmp_path / ".artifacts/output"
    commands = []

    def run(command, **options):
        commands.append(command)
        if command[-1] == "--collect-interrupted":
            if mode == "unavailable":
                raise OSError("Injected subprocess failure")
            atomic_json(
                output / "before-interrupted.json",
                {
                    "usage": {
                        "unknown_calls": 1,
                        "known_usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                        "call_count": 2,
                    }
                },
            )
        elif mode != "complete":
            atomic_json(
                output / "before.json",
                {
                    "results": [
                        {
                            "operation": {"id": "previous", "state": "complete"},
                            "usage": {
                                "unknown_calls": 0,
                                "known_usage": {
                                    "input_tokens": 15,
                                    "output_tokens": 5,
                                    "total_tokens": 20,
                                },
                                "call_count": 1,
                            },
                        }
                    ]
                },
            )
            atomic_json(
                output / "before-pending.json",
                {"operation_id": "previous" if mode == "published" else "interrupted"},
            )
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(comparison.subprocess, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["compare_llm", "--allow-real-llm", "--baseline", str(source), "--output", str(output)],
    )
    with pytest.raises(SystemExit, match="1"):
        comparison.main()
    expected = {
        "complete": ["before", "after"],
        "interrupted": ["before", "--collect-interrupted"],
        "unavailable": ["before", "--collect-interrupted"],
        "published": ["before"],
    }
    assert [command[-1] for command in commands] == expected[mode]
    assert json.loads((output / "sources.json").read_bytes())["before"]["sample.py"]
    fixtures = json.loads((output / "fixtures.json").read_bytes())
    assert {case["id"] for case in fixtures} == {
        "ja-short",
        "en-short",
        "ja-long",
        "en-long",
        "meeting-ja",
        "meeting-en",
    }
    limits = json.loads((output / "budget.json").read_bytes())
    assert limits["minimum_calls"] == 104 < limits["max_calls"]
    completion = json.loads((output / "completion.json").read_bytes())
    assert completion["comparable"] is False
    before, after = (completion["variants"][variant] for variant in ("before", "after"))
    if mode != "complete":
        assert before["status"] == "timed_out" and after["status"] == "not_started"
        assert before["completed_turns"] == 1
        assert before["evidence_complete"] is (mode != "unavailable")
        assert before["total_tokens"] == (20 if mode == "published" else None)
        assert before["calls"] == {"interrupted": 3, "unavailable": None, "published": 1}[mode]
        assert (
            before["unknown_calls"] == {"interrupted": 1, "unavailable": None, "published": 0}[mode]
        )
        assert before["known_input_tokens"] == (22 if mode == "interrupted" else 15)
    else:
        assert before["status"] == after["status"] == "exited"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_llm",
            "--allow-real-llm",
            "--baseline",
            str(source),
            "--output",
            str(tmp_path / "outside"),
        ],
    )
    with pytest.raises(SystemExit):
        comparison.main()
