"""Verify the real-output collector through a declared offline model boundary."""

import json

import pytest
from komaquill.api import Client
from langchain_core.messages import AIMessage

from scripts.acceptance.conversations.actions import collect
from tests.backend.support.conversations import wire_reply

pytestmark = pytest.mark.usefixtures("scripted_services")


def test_failed_operation_keeps_partial_evidence_and_closes_client(tmp_path):
    from komaquill.api import KomaquillError

    with Client.initialize(tmp_path) as client:

        def failed(*args):
            raise KomaquillError(code="llm_unavailable")

        client.conversations.responder.reply = failed
        with pytest.raises(KomaquillError):
            collect(client, tmp_path)
    result = json.loads((tmp_path / "ja.json").read_bytes())
    assert result["status"] == "failed"
    assert result["events"][0]["operation"]["state"] == "failed"
    assert result["failure"]["code"] == "llm_unavailable"


@pytest.mark.parametrize("passed", [True, False])
def test_main_rejects_failed_checks(tmp_path, monkeypatch, passed):
    from scripts.acceptance.conversations import actions

    monkeypatch.setattr(actions, "ROOT", tmp_path)
    monkeypatch.setattr(
        actions, "collect", lambda *args: [{"status": "passed" if passed else "failed"}]
    )
    output = tmp_path / ".artifacts/results"
    assert actions.main(["--allow-real-llm", "--output", str(output)]) == (0 if passed else 1)
    report = json.loads(next(output.glob("*/completion.json")).read_bytes())
    assert report["status"] == ("passed" if passed else "failed")
    assert report["inputs_unchanged"]


def test_collector_tracks_confirmation_memory_and_recreation(tmp_path, monkeypatch):
    import langchain_openai

    calls = []

    class Model:
        def __init__(self, **options):
            self.model = options["model"]

        def bind(self, **options):
            self.summary = (
                "response" not in options["response_format"]["json_schema"]["schema"]["properties"]
            )
            return self

        def invoke(self, messages, config):
            calls.append(messages)
            system = messages[0].content
            ja = "Japanese" in system
            if self.summary:
                value = {
                    "text": "合言葉は銀のコンパス。" if ja else "The password is silver compass."
                }
            else:
                context = next(
                    json.loads(line)
                    for line in system.splitlines()
                    if line.startswith("{") and "current_state" in line
                )
                companion = {
                    key: value
                    for key, value in context["people"]["assistant_character"].items()
                    if key in {"clothing", "expression", "pose", "held_objects"}
                }
                latest = messages[-1].content
                if latest.startswith("あなたは緑のコート") or latest.startswith(
                    "Please wear a green coat"
                ):
                    companion["clothing"] = "Green coat"
                proposals = []
                if latest.startswith("合言葉は銀のコンパスです。") or latest.startswith(
                    "The password is silver compass. I head"
                ):
                    proposals = [{"kind": "move", "destination": "海辺" if ja else "Beach"}]
                value = {
                    "current_state": {
                        "relationship": "",
                        "scenery": "海辺は静かだ。" if ja else "The beach is quiet.",
                        "character": companion,
                    },
                    "action_narration": "",
                    "requested_player_action": proposals[0] if proposals else None,
                    "text": (
                        "合言葉は銀のコンパスです。" if ja else "The password is silver compass."
                    ),
                }
            return AIMessage(
                content=json.dumps(wire_reply(value), ensure_ascii=False),
                response_metadata={"model_name": self.model},
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Model)
    client = Client.initialize(tmp_path)
    (tmp_path / "komaquill.toml").write_text("[conversation]\nrecent_turns=4\n", encoding="utf-8")
    results = collect(client, tmp_path)
    assert len(results) == 2
    assert len(calls) == 10
    for result in results:
        assert all(result["checks"].values())
        assert result["conversation"]["memory"] is None
        assert (tmp_path / f"{result['locale']}.json").is_file()
