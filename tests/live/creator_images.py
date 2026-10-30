"""Verify all image entry points on an explicitly owned Colab T4 session."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from komaquill.api import CharacterInput, Client, CreationRequest, StoryInput
from komaquill.conversations.models import SendInput
from komaquill.domain.status import SessionStatus
from komaquill.library.models import ConversationStart, PresetRef
from komaquill.library.service import sample_id
from pydantic import TypeAdapter


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--allow-real-llm", required=True, action="store_true")
    parser.add_argument("--start-t4", required=True, action="store_true")
    parser.add_argument("--conversation-only", action="store_true")
    args = parser.parse_args()
    api = Client.initialize(args.workspace)
    library = api.library
    report = {"started_at": datetime.now(UTC).isoformat(), "cases": []}
    path = args.workspace / ".artifacts/records/creator-images.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def collect(identity):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            api.images.collect(identity)
            run = api.images.get(identity)
            if run.state in {"saved", "failed", "lost"}:
                return run
            time.sleep(2)
        raise TimeoutError("Image acceptance deadline reached.")

    instance = None
    with httpx.Client(base_url="http://127.0.0.1:1234", timeout=240) as http:
        model = api.conversations.responder.settings.llm_profiles["lmstudio-4b"].model
        selected = next(
            value for value in http.get("/api/v1/models").json()["models"] if value["key"] == model
        )
        if not selected["loaded_instances"]:
            response = http.post(
                "/api/v1/models/load", json={"model": model, "context_length": 16384}
            )
            response.raise_for_status()
            instance = response.json()["instance_id"]
        try:
            print("Starting owned T4 session", flush=True)
            status = api.runtime.start(models=["klein-4b"], gpu="T4", open_browser=False)
            report["runtime"] = TypeAdapter(SessionStatus).dump_python(status, mode="json")
            save()
            print("T4 ready", flush=True)
            character = api.characters.create(
                CharacterInput(
                    name="栞",
                    appearance="短い銀髪、黒いエプロンの成人女性。",
                    personality="穏やかな古書店主。",
                )
            )
            story = api.stories.create(
                StoryInput(
                    name="雨の日の古書店",
                    world="港町の古書店。雨の午後。",
                    opening="雨宿りのために古書店のカウンターを訪れた。",
                    place="古書店の店内",
                    goal="青い封筒の手がかりを探す",
                    character_id=character.id,
                )
            )
            for kind, value in (
                [] if args.conversation_only else [("portrait", character), ("banner", story)]
            ):
                request = CreationRequest(
                    operation_id=uuid4(),
                    kind=kind,
                    target_id=value.id,
                    expected_revision=0,
                    locale="ja",
                    llm_profile="lmstudio-4b",
                    allow_real_llm=True,
                )
                library.accept(request)
                job = library.execute(request.operation_id)
                if job.image_id:
                    collect(job.image_id)
                job = library.job(job.id)
                report["cases"].append({"kind": kind, "job": job.model_dump(mode="json")})
                save()
                print(kind, job.state, job.error, flush=True)
            conversation = library.start(
                ConversationStart(
                    player=PresetRef(id=sample_id("ja", "player"), revision=0),
                    character=PresetRef(id=character.id, revision=0),
                    story=PresetRef(id=story.id, revision=0),
                    locale="ja",
                    llm_profile="lmstudio-4b",
                    images=True,
                )
            )
            operation = api.conversations.send(
                conversation.id,
                SendInput(
                    operation_id=uuid4(),
                    text="カウンターの前で、あなたが窓辺の本を指さして紹介する場面を描いてください。",
                    allow_real_llm=True,
                    images=True,
                ),
            )
            if operation.image_id:
                collect(operation.image_id)
            report["cases"].append(
                {
                    "kind": "conversation",
                    "operation": operation.model_dump(mode="json"),
                    "conversation": api.conversations.get(conversation.id).model_dump(mode="json"),
                }
            )
            save()
            print("conversation", operation.state, operation.image_state, flush=True)
            if not args.conversation_only:
                run = api.images.generate(
                    "雨の港町にある古書店の窓辺。木製の本棚と青い封筒。人物なし。",
                    seed=417,
                    llm_profile="lmstudio-4b",
                    allow_real_llm=True,
                )
                report["cases"].append({"kind": "standalone", "run": run.model_dump(mode="json")})
                save()
                print("standalone", run.state, flush=True)
            report["images"] = [run.model_dump(mode="json") for run in api.images.list()]
            assert operation.state == "complete" and operation.image_state == "submitted"
            assert operation.image_id and api.images.get(operation.image_id).state == "saved"
        finally:
            try:
                report["shutdown"] = TypeAdapter(SessionStatus).dump_python(
                    api.runtime.stop(), mode="json"
                )
            finally:
                if instance:
                    report["model_unloaded"] = http.post(
                        "/api/v1/models/unload", json={"instance_id": instance}
                    ).is_success
                report["finished_at"] = datetime.now(UTC).isoformat()
                save()


if __name__ == "__main__":
    main()
