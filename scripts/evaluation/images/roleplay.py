"""Collect real output separately from deterministic tests and retain failures before cleanup."""

import argparse
import hashlib
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from komaquill.api import (
    CharacterInput,
    Client,
    ConversationStart,
    CreationRequest,
    KomaquillError,
    PlayerInput,
    PresetRef,
    PresetSaveOptions,
    SendInput,
    SessionStatus,
    StoryInput,
)
from pydantic import TypeAdapter

from scripts.acceptance import budget
from scripts.acceptance.run import LIMITS
from tools.reporting import atomic_json, input_fingerprint, message, source_revision

ROOT = Path(__file__).resolve().parents[3]
INPUTS = {
    "ja": [
        "私は旅人の灯です。帰りに灯台の青い扉を見る約束をしてください。",
        "あなたの名前と年齢を教えてください。",
        "海へ行きたい。一緒に海まで行きましょう。",
        "ここは海です。海を見た感想を聞かせてください。",
        "私の赤いコートは私が着ています。あなたは黒いエプロンのままです。",
        "私の赤いコートを預かってください。私はコートを脱ぎます。",
        "赤いコートを返してください。私はそれを着ます。",
        "あなたは青いシャツに着替えて、岩に腰掛けてください。",
        "そのまま、好きな音楽の話をしてください。",
        "その姿勢のままで、髪型の話を聞かせてください。",
        "波の音を聞いて、今どんな気持ちですか。",
        "海辺で好きな食べ物の話をしましょう。",
        "最初に、帰りに何を見る約束をしましたか。場所と色を教えてください。",
        "私とあなたはそれぞれ何を着ていますか。今どこにいますか。",
    ],
    "en": [
        "I am the traveler Akari. Promise we will see the blue door "
        "of the lighthouse on our way back.",
        "Tell me your name and age.",
        "I want to go to the sea. Let us go to the beach together now.",
        "We are at the sea now. What do you think of the view?",
        "I am wearing my red coat. You are still wearing your black apron.",
        "Please hold my red coat for me. I take it off.",
        "Return my red coat, please. I put it on again.",
        "Change into a blue shirt and sit on a rock, please.",
        "Stay like that and tell me about your favorite music.",
        "Keep your position and tell me about your hairstyle.",
        "How do you feel listening to the waves?",
        "Let us talk about food we like at the beach.",
        "What did we promise to see on our way back? Tell me the place and the color.",
        "What are you wearing and what am I wearing? Where are we now?",
    ],
}


def collect_turn(
    api: Any, identity: UUID, locale: str, text: str, report: dict[str, Any], record: Path
) -> None:
    operation = api.conversations.send(
        identity,
        SendInput(
            expected_revision=api.conversations.overview(identity).revision,
            operation_id=uuid4(),
            text=text,
            locale=cast(Literal["ja", "en"], locale),
            allow_real_llm=True,
        ),
    )
    conversation = api.conversations.get(identity)
    report["turns"].append(
        {
            "locale": locale,
            "input": text,
            "operation": operation.model_dump(mode="json"),
            "conversation": conversation.model_dump(mode="json"),
        }
    )
    atomic_json(record, report)
    print(message("review.turn_saved", locale=locale, count=len(report["turns"])), flush=True)
    if operation.state != "complete":
        raise KomaquillError(code=operation.error or "operation_failed")


def conversations(api: Any, report: dict[str, Any], record: Path) -> None:
    player = api.player.update(
        api.player.get().revision,
        PlayerInput(
            name="Akari",
            age="28",
            gender="woman",
            appearance="An adult traveler wearing a red coat.",
        ),
    )
    api.library.save_preferences(
        api.library.preferences().model_copy(update={"response_length": "short"})
    )
    report["selections"] = []
    for locale, prompts in INPUTS.items():
        character = api.characters.create(
            CharacterInput(
                name="Mira",
                age="60",
                gender="man",
                appearance=(
                    "A sixty-year-old man with short silver hair, wrinkles and a black apron."
                ),
                role="Bookshop owner",
                personality="Warm and curious",
                speech="Natural and concise",
            )
        )
        story = api.stories.create(
            StoryInput(
                name=f"Seaside walk ({locale})",
                world=(
                    "A contemporary seaside town. The bookshop and beach "
                    "are within walking distance."
                ),
                place="Bookshop",
                opening="A traveler meets the shopkeeper on a quiet afternoon.",
                character_id=character.id,
            )
        )
        conversation = api.library.start(
            ConversationStart(
                player_revision=player.revision,
                character=PresetRef(id=character.id, revision=character.revision),
                story=PresetRef(id=story.id, revision=story.revision),
                locale=cast(Literal["ja", "en"], locale),
            )
        )
        report["selections"].append(
            {
                "locale": locale,
                "conversation": str(conversation.id),
                "character": str(character.id),
                "story": str(story.id),
            }
        )
        for index, text in enumerate(prompts):
            if index == len(prompts) - 1:
                api = Client(api.workspace)
            collect_turn(api, conversation.id, locale, text, report, record)


def save_image(api: Any, identity: UUID, kind: str, report: dict[str, Any], record: Path) -> None:
    job = api.library.job(identity) if kind != "standalone" else None
    if job is not None and job.state in {"failed", "interrupted"}:
        report.setdefault("failed_jobs", []).append(job.model_dump(mode="json"))
        atomic_json(record, report)
        raise KomaquillError(code=job.error or "operation_failed")
    run = api.images.wait(identity)
    api.library.reconcile()
    report["images"].append(
        {
            "kind": kind,
            "run": run.model_dump(mode="json"),
            "files": [
                {
                    "key": f"runs/{identity}/{name}",
                    "sha256": hashlib.sha256(
                        api.artifact_path(f"runs/{identity}/{name}").read_bytes()
                    ).hexdigest(),
                }
                for name in run.images
            ],
        }
    )
    atomic_json(record, report)
    if run.state != "saved" or not run.images:
        raise KomaquillError(code="generation_failed")


def render_preset(api: Any, preset: Any, kind: str, locale: str) -> UUID:
    request = CreationRequest(
        operation_id=uuid4(),
        kind="portrait" if kind == "character" else "banner",
        target_id=preset.id,
        expected_revision=preset.revision,
        locale=cast(Literal["ja", "en"], locale),
        allow_real_llm=True,
    )
    budget.reserve(api.workspace, str(request.operation_id))
    job, created = api.library.accept(request)
    if created:
        job = api.library.execute(job.id)
    if job.image_id is None:
        raise KomaquillError(code=job.error or "operation_failed")
    return UUID(str(job.id))


def images(api: Any, report: dict[str, Any], record: Path) -> None:
    for selection in report["selections"]:
        if selection["locale"] == "ja":
            for kind in ("character", "story"):
                service = api.characters if kind == "character" else api.stories
                saved = service.get(UUID(selection[kind]))
                model = CharacterInput if kind == "character" else StoryInput
                value = model.model_validate(saved.model_dump(include=set(model.model_fields)))
                result = api.library.save_preset(
                    kind,
                    value,
                    PresetSaveOptions(
                        operation_id=uuid4(),
                        target_id=saved.id,
                        expected_revision=saved.revision,
                    ),
                )
                save_image(
                    api,
                    render_preset(api, result.preset, kind, selection["locale"]),
                    kind,
                    report,
                    record,
                )
        conversation = api.conversations.get(UUID(selection["conversation"]))
        request = CreationRequest(
            operation_id=uuid4(),
            kind="conversation_image",
            target_id=conversation.id,
            expected_revision=conversation.revision,
            locale=selection["locale"],
            allow_real_llm=True,
        )
        budget.reserve(api.workspace, str(request.operation_id))
        api.library.accept(request)
        job = api.library.execute(request.operation_id)
        if job.image_id is None:
            raise KomaquillError(code=job.error or "operation_failed")
        save_image(api, job.id, "conversation_image", report, record)
    female = api.library.save_preset(
        "character",
        CharacterInput(
            name="Noa",
            age="32",
            gender="woman",
            appearance="A 32-year-old woman with short red hair, wearing a green coat.",
        ),
        PresetSaveOptions(
            operation_id=uuid4(),
        ),
    )
    save_image(
        api, render_preset(api, female.preset, "character", "ja"), "portrait", report, record
    )
    budget.reserve(api.workspace, str(uuid4()))
    run = api.images.submit(
        "静かな海辺。青い空と白い波。人物はいない。", llm_profile="lmstudio-4b", allow_real_llm=True
    )
    save_image(api, run.run_id, "standalone", report, record)


def main() -> None:
    parser = argparse.ArgumentParser(description=message("review.roleplay_description"))
    parser.add_argument(
        "--allow-real-llm", action="store_true", required=True, help=message("review.allow_llm")
    )
    parser.add_argument("--allow-gpu", action="store_true")
    parser.add_argument(
        "--phase",
        choices=("conversations", "images"),
        default="conversations",
        help=message("review.phase"),
    )
    parser.add_argument("--workspace", type=Path, help=message("review.workspace"))
    args = parser.parse_args()
    if args.phase == "images" and args.workspace is None:
        parser.error(message("review.workspace_required"))
    if args.phase == "images" and not args.allow_gpu:
        parser.error(message("review.gpu_permission_required"))
    directory = ROOT / ".artifacts/records/reports" / f"roleplay-{uuid4().hex}"
    workspace = (args.workspace or directory / "workspace").resolve()
    if not workspace.is_relative_to(ROOT / ".artifacts"):
        parser.error(message("review.workspace_required"))
    api = Client(workspace) if args.workspace else Client.initialize(workspace)
    api.limit_llm_calls(workspace.parent / "allowance.sqlite3", **LIMITS["llm"])
    if args.workspace is None:
        settings = Path(__file__).with_name("budget.toml").read_text(encoding="utf-8")
        tomllib.loads(settings)
        (workspace / "komaquill.toml").write_text(settings, encoding="utf-8")
    report: dict[str, Any] = {
        "format_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "source_revision": source_revision(ROOT),
        "input_sha256": input_fingerprint(ROOT),
        "workspace": str(workspace),
        "phase": args.phase,
        "review_status": "pending",
        "functional_status": "running",
        "turns": [],
        "images": [],
    }
    record = directory / "evidence.json"
    atomic_json(record, report)
    owned = False
    cleanup_failed = False
    allocation = str(uuid4())
    try:
        if args.phase == "images":
            previous = json.loads((workspace.parent / "evidence.json").read_bytes())
            report["selections"] = previous["selections"]
            report["parent_input_sha256"] = previous["input_sha256"]
            before = api.runtime.status()
            if (
                before.gpu not in {"absent", "stopped"}
                or before.collector_running
                or before.connection == "connected"
            ):
                raise KomaquillError(code="operation_busy", retryable=False)
            usage = api.runtime.usage()
            if usage.assignments_count:
                raise KomaquillError(code="operation_busy")
            budget.open_budget(workspace, LIMITS["gpu"], usage)
            send, stop = budget.begin(workspace, allocation)
            budget.configure_deadlines(workspace, send, stop)
            api.close()
            api = Client(workspace)
            owned = True
            report["runtime"] = TypeAdapter(SessionStatus).dump_python(
                api.runtime.start(models=["wai-illustrious"], gpu="T4", open_browser=False),
                mode="json",
            )
            images(api, report, record)
        else:
            conversations(api, report, record)
        report["functional_status"] = "passed"
    except BaseException as error:
        report["functional_status"] = "failed"
        report["error"] = {"type": type(error).__name__, "code": getattr(error, "code", None)}
        raise
    finally:
        if owned:
            try:
                stopped = api.runtime.stop()
                report["shutdown"] = TypeAdapter(SessionStatus).dump_python(stopped, mode="json")
                cleanup_failed = stopped.gpu not in {"stopped", "absent"}
                if not cleanup_failed:
                    budget.checkpoint(
                        workspace, allocation, stopped_at=datetime.now(UTC), released=True
                    )
            except Exception as error:
                cleanup_failed = True
                report["shutdown"] = {"confirmed": False, "type": type(error).__name__}
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["inputs_unchanged"] = input_fingerprint(ROOT) == report["input_sha256"]
        if cleanup_failed:
            report["functional_status"] = "failed"
            report["cleanup_error"] = "shutdown_unconfirmed"
        if not report["inputs_unchanged"]:
            report["functional_status"] = "inputs_changed"
        atomic_json(record, report)
        api.close()
        print(message("review.evidence_saved", path=record))
    if cleanup_failed:
        raise KomaquillError(code="shutdown_timeout")
    if not report["inputs_unchanged"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
