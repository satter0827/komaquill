"""Own bounded live model and image checks, retaining evidence before teardown."""

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from komaquill.adapters.atomic import atomic_json
from komaquill.api import (
    CharacterInput,
    Client,
    ConversationStart,
    CreationRequest,
    PlayerInput,
    PresetRef,
    SendInput,
    StoryInput,
)
from komaquill.domain.status import SessionStatus
from pydantic import TypeAdapter

from scripts._support import input_fingerprint, source_revision


def verify_conversations(api, report, record):
    api.player.update(
        api.player.get().revision,
        PlayerInput(
            name="灯",
            address="灯さん",
            age="28",
            gender="女性",
            role="旅人",
            appearance="赤いコート",
        ),
    )
    cases = [
        (
            CharacterInput(
                name="Noah",
                age="41",
                gender="man",
                appearance="An adult man with short black hair and a silver station uniform.",
                role="Orbital greenhouse navigator",
            ),
            StoryInput(
                name="Orbital greenhouse",
                world="An orbital station with a sealed greenhouse. No magic.",
                opening="A visitor meets the navigator at the greenhouse entrance.",
                place="Greenhouse entrance",
            ),
            [
                ("en", "Hello. Tell me your name, age and work."),
                ("ja", "温室の様子を教えてください。"),
                ("en", "Stay at the entrance and describe what you can see."),
            ],
        ),
        (
            CharacterInput(
                name="ミラ",
                age="32",
                gender="女性",
                appearance="短い赤茶色の髪、緑のコート。",
                role="港町の古書店主",
            ),
            StoryInput(
                name="雨の古書店", opening="雨の午後、旅人が古書店を訪れた。", place="古書店"
            ),
            [
                ("ja", "Hello. Tell me about yourself and this shop."),
                ("en", "青いシャツになり、庭の椅子に座ってください。"),
                ("ja", "Stay in the garden and describe the flowers."),
            ],
        ),
    ]
    for character_input, story_input, turns in cases:
        character = api.characters.create(character_input)
        story = api.stories.create(story_input.model_copy(update={"character_id": character.id}))
        conversation = api.library.start(
            ConversationStart(
                player_revision=api.player.get().revision,
                character=PresetRef(id=character.id, revision=0),
                story=PresetRef(id=story.id, revision=0),
                locale=turns[0][0],
            )
        )
        for locale, text in turns:
            operation = api.conversations.send(
                conversation.id,
                SendInput(operation_id=uuid4(), text=text, locale=locale, allow_real_llm=True),
            )
            report["cases"].append(
                {
                    "kind": "conversation",
                    "locale": locale,
                    "operation": operation.model_dump(mode="json"),
                    "conversation": api.conversations.get(conversation.id).model_dump(mode="json"),
                }
            )
            atomic_json(record, report)
            assert operation.state == "complete"
    report["conversations"] = [value.model_dump(mode="json") for value in api.conversations.list()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-real-llm", required=True, action="store_true")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--start-t4", action="store_true")
    scope.add_argument("--conversation-only", action="store_true")
    scope.add_argument("--banner-only", action="store_true")
    parser.add_argument("--image-evidence", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    directory = root / ".artifacts/records/reports" / ("play-live-" + uuid4().hex)
    api = Client.initialize(directory / "workspace")
    model = api.conversations.responder.settings.llm_profiles["lmstudio-4b"].model
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "source_revision": source_revision(root),
        "input_sha256": input_fingerprint(root),
        "model": model,
        "real_llm": True,
        "real_images": args.start_t4 or args.banner_only,
        "scope": "banner-only"
        if args.banner_only
        else "conversations-only"
        if args.conversation_only
        else "full-browser"
        if args.start_t4
        else "drafts-only",
        "status": "running",
        "cases": [],
    }
    record = directory / "live.json"
    atomic_json(record, report)
    instance = None
    gpu_owned = False
    server = None
    output = None
    with httpx.Client(base_url="http://127.0.0.1:1234", timeout=300) as http:
        try:
            models = http.get("/api/v1/models").json()["models"]
            selected = next(item for item in models if item["key"] == model)
            if not selected["loaded_instances"]:
                result = http.post(
                    "/api/v1/models/load", json={"model": model, "context_length": 16384}
                )
                result.raise_for_status()
                instance = result.json()["instance_id"]
            drafts = [
                (
                    "ja",
                    "栞。32歳の女性。短い銀髪と黒いエプロンの穏やかな古書店主。"
                    "雨の港町の店で旅人を迎える。",
                ),
                (
                    "en",
                    "Shiori, a 32-year-old woman with short silver hair and a black apron. "
                    "A calm bookshop owner welcomes a visitor "
                    "on a rainy afternoon in a seaside town.",
                ),
            ]
            for locale, prompt in [] if args.conversation_only or args.banner_only else drafts:
                for kind in ("character_draft", "story_draft"):
                    request = CreationRequest(
                        operation_id=uuid4(),
                        kind=kind,
                        prompt=prompt,
                        locale=locale,
                        allow_real_llm=True,
                    )
                    api.library.accept(request)
                    job = api.library.execute(request.operation_id)
                    report["cases"].append(job.model_dump(mode="json"))
                    atomic_json(record, report)
                    assert job.state == "complete" and job.result is not None
                    assert (
                        job.result.appearance if kind == "character_draft" else job.result.opening
                    )
                    if kind == "character_draft":
                        assert job.result.age == "32" and job.result.gender
            if args.start_t4 or args.banner_only:
                gpu_owned = True
                report["runtime"] = TypeAdapter(SessionStatus).dump_python(
                    api.runtime.start(models=["klein-4b"], gpu="T4", open_browser=False),
                    mode="json",
                )
                atomic_json(record, report)
            if args.banner_only:
                story = api.stories.create(
                    StoryInput(
                        name="雨の古書店",
                        opening="雨の午後、港町の古書店に旅人が入ってきた。店主がカウンターの前で迎える。",
                    )
                )
                job, _ = api.library.accept(
                    CreationRequest(
                        operation_id=uuid4(),
                        kind="banner",
                        target_id=story.id,
                        expected_revision=0,
                        allow_real_llm=True,
                    )
                )
                submitted = api.library.execute(job.id)
                report["cases"].append(submitted.model_dump(mode="json"))
                atomic_json(record, report)
                assert submitted.state == "submitted"
                api.images.wait(job.id)
                api.images.collect(job.id)
                completed = api.library.job(job.id)
                report["cases"].append(completed.model_dump(mode="json"))
                report["images"] = [value.model_dump(mode="json") for value in api.images.list()]
                atomic_json(record, report)
                assert (
                    completed.state == "complete" and api.stories.get(story.id).image_id == job.id
                )
            if args.start_t4:
                output = (directory / "server.log").open("w", encoding="utf-8")
                server = subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "tests.support.live_web_server",
                        "--workspace",
                        str(api.workspace),
                    ],
                    cwd=root,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    assert server.poll() is None
                    try:
                        if httpx.get("http://127.0.0.1:8767", timeout=1).is_success:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
                result = subprocess.run(
                    ["node", "scripts/web/live-review.mjs", str(directory)], cwd=root, check=False
                )
                assert result.returncode == 0
                report["images"] = [value.model_dump(mode="json") for value in api.images.list()]
                assert len(report["images"]) >= 5 and all(
                    value["state"] == "saved" for value in report["images"]
                )
            if args.start_t4 or args.conversation_only:
                verify_conversations(api, report, record)
            if args.image_evidence:
                previous = args.image_evidence.resolve()
                assert previous.is_relative_to(root / ".artifacts/records/reports")
                saved = Client(previous / "workspace")
                jobs = [
                    job
                    for conversation in saved.conversations.list()
                    for job in saved.library.conversation_images(conversation.id)
                ]
                selected_job = max(jobs, key=lambda job: job.created_at)
                source = (
                    previous
                    / "workspace/.artifacts/data/conversation-calls"
                    / str(selected_job.id)
                    / "image-0.json"
                )
                original = json.loads(source.read_bytes())
                text = original["messages"][-1]["content"].removesuffix(
                    "\n\nWrite the image description in English only."
                )
                identity = uuid4()
                prompt = api.library.creator.prepare_image(
                    text, identity, "lmstudio-4b", True, compose=True, purpose="conversation_image"
                )
                observed = json.loads(
                    (
                        api.workspace
                        / ".artifacts/data/conversation-calls"
                        / str(identity)
                        / "image-0.json"
                    ).read_bytes()
                )
                same = all(
                    observed[key] == original[key]
                    for key in ("schema", "messages", "instructions_sha256")
                )
                report["image_input_recheck"] = {
                    "source": str(source),
                    "operation_id": str(identity),
                    "same_wire_contract": same,
                    "prompt_en": prompt,
                }
                atomic_json(record, report)
                assert same
            report["status"] = "passed"
        except BaseException as error:
            report["status"] = "failed"
            report["error"] = {"type": type(error).__name__, "code": getattr(error, "code", None)}
            raise
        finally:
            cleanup_errors = []
            try:
                if server is not None:
                    server.terminate()
                    server.wait(timeout=15)
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
            if output is not None:
                output.close()
            try:
                if gpu_owned:
                    report["shutdown"] = TypeAdapter(SessionStatus).dump_python(
                        api.runtime.stop(), mode="json"
                    )
                    if report["shutdown"]["gpu"] not in {"stopped", "absent"}:
                        cleanup_errors.append("GpuShutdownUnconfirmed")
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
            try:
                if instance:
                    report["model_unloaded"] = http.post(
                        "/api/v1/models/unload", json={"instance_id": instance}
                    ).is_success
                    if not report["model_unloaded"]:
                        cleanup_errors.append("ModelUnloadFailed")
                report["models_after"] = [
                    {"key": item["key"], "loaded_instances": item["loaded_instances"]}
                    for item in http.get("/api/v1/models").json()["models"]
                ]
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
            report["cleanup_errors"] = cleanup_errors
            report["finished_at"] = datetime.now(UTC).isoformat()
            report["inputs_unchanged"] = input_fingerprint(root) == report["input_sha256"]
            if cleanup_errors or not report["inputs_unchanged"]:
                report["status"] = "failed"
            atomic_json(record, report)
            print(record)
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
