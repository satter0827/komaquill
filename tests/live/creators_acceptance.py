"""Run explicitly authorized creator and language checks against the local model."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from komaquill.api import CharacterInput, Client, CreationRequest, KomaquillError, StoryInput
from komaquill.conversations.models import SendInput
from komaquill.library.models import ConversationStart, PresetRef
from komaquill.library.service import sample_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--allow-real-llm", action="store_true", required=True)
    parser.add_argument("--skip-drafts", action="store_true")
    args = parser.parse_args()
    api = Client.initialize(args.workspace)
    library = api.library
    model = api.conversations.responder.settings.llm_profiles["lmstudio-4b"].model
    report = {"started_at": datetime.now(UTC).isoformat(), "model": model, "cases": []}
    target = args.workspace / ".artifacts/records/creators-llm.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    instance = None
    with httpx.Client(base_url="http://127.0.0.1:1234", timeout=240) as http:
        models = http.get("/api/v1/models").json()["models"]
        selected = next(value for value in models if value["key"] == model)
        if not selected["loaded_instances"]:
            response = http.post(
                "/api/v1/models/load", json={"model": model, "context_length": 16384}
            )
            response.raise_for_status()
            instance = response.json()["instance_id"]
        try:
            for locale, idea in [
                ("ja", "港町の古書店。短い銀髪と黒いエプロンの店主、栞。穏やかな成人女性。"),
                (
                    "en",
                    "A quiet seaside bookshop called Tidebound Books. "
                    "The adult shopkeeper has short silver hair and a black apron. "
                    "Keep the shopkeeper's name exactly as 栞.",
                ),
            ]:
                for kind in () if args.skip_drafts else ("character_draft", "story_draft"):
                    request = CreationRequest(
                        operation_id=uuid4(),
                        kind=kind,
                        prompt=idea,
                        locale=locale,
                        llm_profile="lmstudio-4b",
                        allow_real_llm=True,
                    )
                    library.accept(request)
                    job = library.execute(request.operation_id)
                    preset = None
                    if job.state == "complete" and job.result is not None:
                        preset = (
                            api.characters.create(CharacterInput.model_validate(job.result))
                            if kind == "character_draft"
                            else api.stories.create(StoryInput.model_validate(job.result))
                        )
                    report["cases"].append(
                        {
                            "kind": kind,
                            "locale": locale,
                            "result": job.model_dump(mode="json"),
                            "preset": preset.model_dump(mode="json")
                            if preset is not None
                            else None,
                        }
                    )
                    save()
                    print(kind, locale, job.state, job.error, flush=True)
            request = ConversationStart(
                **{
                    kind: PresetRef(id=sample_id("ja", kind), revision=0)
                    for kind in ("player", "character", "story")
                },
                locale="ja",
                llm_profile="lmstudio-4b",
            )
            conversation = library.start(request)
            for locale, text in [
                ("ja", "Hello. What kind of book would you recommend?"),
                ("en", "ありがとう。この店の雰囲気について教えて。"),
                ("ja", "Please tell me about the blue envelope in my hand."),
            ]:
                operation = api.conversations.send(
                    conversation.id,
                    SendInput(operation_id=uuid4(), text=text, locale=locale, allow_real_llm=True),
                )
                report["cases"].append(
                    {
                        "kind": "conversation",
                        "locale": locale,
                        "operation": operation.model_dump(mode="json"),
                        "conversation": api.conversations.get(conversation.id).model_dump(
                            mode="json"
                        ),
                    }
                )
                save()
                print("conversation", locale, operation.state, operation.error, flush=True)
            reply = api.conversations.responder.reply(
                api.conversations.get(conversation.id),
                SendInput(
                    operation_id=uuid4(),
                    text="栞が窓辺の本を指さす場面を描いてください。",
                    locale="ja",
                    images=True,
                    allow_real_llm=True,
                ),
            )
            report["cases"].append(
                {
                    "kind": "image_description",
                    "locale": "ja",
                    "reply": reply.model_dump(mode="json"),
                }
            )
            save()
            assert all(
                case.get("result", case.get("operation", {})).get("state") == "complete"
                for case in report["cases"]
                if case["kind"] != "image_description"
            )
        except KomaquillError as error:
            report["error"] = error.code.value
            raise
        finally:
            if instance:
                response = http.post("/api/v1/models/unload", json={"instance_id": instance})
                report["unloaded"] = response.is_success
            report["models_after"] = http.get("/api/v1/models").json()
            report["finished_at"] = datetime.now(UTC).isoformat()
            save()


if __name__ == "__main__":
    main()
