"""Verify the real-output collector through a declared offline model boundary."""

import json

import pytest
from komaquill.api import Client
from langchain_core.messages import AIMessage

from scripts.verify_player_actions import collect
from tests.support.conversations import wire_reply

pytestmark = pytest.mark.usefixtures("substituted_services")


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
                current = context["current_state"]
                companion = {
                    key: value
                    for key, value in context["people"]["assistant_character"].items()
                    if key in {"clothing", "expression", "pose", "held_objects"}
                }
                latest = messages[-1].content
                char_place = current["character_place"]
                if latest.startswith("あなたは緑のコート") or latest.startswith(
                    "Please wear a green coat"
                ):
                    companion["clothing"] = "Green coat"
                    char_place = current["place"]
                proposals = []
                if latest.startswith("合言葉は銀のコンパスです。") or latest.startswith(
                    "The password is silver compass. I head"
                ):
                    proposals = [{"kind": "move", "destination": "海辺" if ja else "Beach"}]
                value = {
                    "current_state": {
                        "relationship": "",
                        "companion_movement": "join_player"
                        if char_place == current["place"]
                        else "stay",
                        "scenery": "海辺は静かだ。" if ja else "The beach is quiet.",
                        "character": companion,
                    },
                    "action_narration": "",
                    "requested_player_action": proposals[0] if proposals else None,
                    "text": (
                        "合言葉は銀のコンパスです。" if ja else "The password is silver compass."
                    )
                    if char_place == current["place"]
                    else "",
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
