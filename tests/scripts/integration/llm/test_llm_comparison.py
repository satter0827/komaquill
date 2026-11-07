"""Exercise collection and its budgets with paired fake responses, never a live model."""

import json
import sys
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from scripts.evaluation.llm import compare as comparison
from tests.backend.support.conversations import wire_reply
from tools.reporting import atomic_json

pytestmark = pytest.mark.usefixtures("scripted_services")


def budget(path, *, max_calls=52, deadline=None):
    atomic_json(
        path, {"max_calls": max_calls, "deadline": deadline or time.time() + 60, "calls": []}
    )


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


@pytest.mark.parametrize("failed", [False, True])
def test_full_child_uses_explicit_permission_and_saves_actual_failures(
    tmp_path, monkeypatch, failed
):
    import langchain_openai

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **options):
            self.summary = (
                "response" not in options["response_format"]["json_schema"]["schema"]["properties"]
            )
            return self

        def invoke(self, messages, *args, **kwargs):
            if failed:
                raise RuntimeError("Injected failure")
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
                content=json.dumps(wire_reply(response), ensure_ascii=False),
                response_metadata={"model_name": self.model},
                usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    original_path = list(sys.path)
    output = tmp_path / "output"
    output.mkdir()
    budget(output / "budget.json")
    atomic_json(
        output / "fixtures.json",
        [comparison.fixture(locale, long, 14) for locale in ("ja", "en") for long in (False, True)],
    )
    try:
        comparison.child(comparison.ROOT / "src/backend/src", output, "after")
    finally:
        sys.path[:] = original_path
    collected = json.loads((output / "after.json").read_bytes())
    calls = collected["calls"]
    rows = collected["results"]
    assert len(calls) == (4 if failed else 40)
    assert all(item["status"] == ("failed" if failed else "response_received") for item in calls)
    assert len(rows) == (4 if failed else 40)
    assert all(item["operation"]["state"] == ("failed" if failed else "complete") for item in rows)
    if not failed:
        assert all(item["usage"]["total_tokens"] == 20 for item in calls)
        assert rows[-1]["conversation"]["memory"] is None


def test_parent_creates_frozen_inputs_and_uses_separate_interpreters(tmp_path, monkeypatch):
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
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(comparison.subprocess, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["compare_llm", "--allow-real-llm", "--baseline", str(source), "--output", str(output)],
    )
    comparison.main()
    assert len(commands) == 2 and commands[0][-1] == "before" and commands[1][-1] == "after"
    assert json.loads((output / "sources.json").read_bytes())["before"]["sample.py"]
    assert len(json.loads((output / "fixtures.json").read_bytes())) == 4
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
