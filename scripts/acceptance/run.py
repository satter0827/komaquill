"""Run explicitly authorized model and owned-GPU acceptance through the public API."""

import argparse
import json
import os
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from komaquill.api import (
    CharacterInput,
    Client,
    ConversationInput,
    CreationRequest,
    KomaquillError,
    PrepareAction,
    SendInput,
    StoryInput,
    describe_failure,
)

from scripts.acceptance import budget
from tools.observations import owner
from tools.reporting import atomic_json, input_fingerprint, message, source_revision
from tools.runtime.processes import owned_process, run_owned
from tools.testing.browser_environment import prepare

ROOT = Path(__file__).resolve().parents[2]
LIMITS = tomllib.loads(Path(__file__).with_name("budget.toml").read_text(encoding="utf-8"))
RUNTIME = tomllib.loads(Path(__file__).with_name("workspace.toml").read_text(encoding="utf-8"))
LIMITS["gpu"].update(
    compute_unit_limit=RUNTIME["compute_unit_limit"],
    compute_unit_reserve=RUNTIME["compute_unit_reserve"],
    session_seconds=int(RUNTIME["session_timeout"]),
    submission_seconds=int(RUNTIME["submission_timeout"]),
)


def attach(
    api: Client,
    output: Path,
    *,
    ledger: Path | None = None,
    max_calls: int | None = None,
    max_seconds: int | None = None,
) -> None:
    api.limit_llm_calls(
        ledger or output.parent / "allowance.sqlite3",
        max_calls=LIMITS["llm"]["max_calls"] if max_calls is None else max_calls,
        max_seconds=LIMITS["llm"]["max_seconds"] if max_seconds is None else max_seconds,
        create=ledger is None,
    )


def conversations(api: Client, output: Path, report: dict[str, Any]) -> tuple[Client, list[UUID]]:
    identities = []
    for locale in ("ja", "en"):
        selected: Literal["ja", "en"] = locale
        conversation = api.conversations.create(
            ConversationInput.model_validate(
                {
                    "title": f"Acceptance {locale}",
                    "locale": selected,
                    "user": {"name": "Akari", "age": "28"},
                    "character": {"name": "Mira", "age": "32", "appearance": "Short silver hair"},
                    "scenario": {"place": "Bookshop", "world": "A contemporary harbor town"},
                }
            )
        )
        identities.append(conversation.id)
        for index, text in enumerate(
            ("こんにちは。あなたの名前を教えてください。", "私は赤いコートを着て立ちます。")
            if locale == "ja"
            else ("Hello. What is your name?", "I put on a red coat and stand up.")
        ):
            request = SendInput(
                operation_id=uuid4(),
                expected_revision=api.conversations.overview(conversation.id).revision,
                text=text,
                locale=selected,
                allow_real_llm=True,
                action=PrepareAction(clothing="Red coat", pose="Standing") if index else None,
            )
            result = api.conversations.send(conversation.id, request)
            row: dict[str, Any] = {
                "case": f"conversation-{locale}-{index}",
                "operation_id": str(result.id),
                "state": result.state,
                "error": result.error,
                "usage": api.operation_usage(result.id),
            }
            report["cases"].append(row)
            atomic_json(output / "run.json", report)
            if result.state != "complete":
                raise KomaquillError(code=result.error or "operation_failed")
            records = api.llm_records(result.id)
            assert records and all(record.get("token_counting") == "lmstudio" for record in records)
            calls = api.llm_usage()["reserved_calls"]
            api.close()
            api = Client(api.workspace)
            assert api.conversations.send(conversation.id, request).state == "complete"
            assert api.llm_usage()["reserved_calls"] == calls
            assert api.conversations.get(conversation.id).turn_count == index + 1
            row["reopened_without_resend"] = True
    return api, identities


def gpu(
    api: Client,
    output: Path,
    report: dict[str, Any],
    identities: list[UUID],
    *,
    saved_only: bool = False,
    time_extension: tuple[UUID, int] | None = None,
    conversation_only: bool = False,
) -> None:
    before = api.runtime.status()
    account = api.runtime.usage()
    if (
        before.gpu not in {"absent", "stopped"}
        or before.collector_running
        or account.assignments_count != 0
    ):
        raise KomaquillError(code="operation_busy")
    report["gpu"] = {"state": "starting", "initial_usage": account.model_dump(mode="json")}
    budget.open_budget(api.workspace, LIMITS["gpu"], account)
    if time_extension:
        budget.extend_time(api.workspace, *time_extension)
    allocation = str(uuid4())
    send, stop = budget.begin(
        api.workspace,
        allocation,
        reserve_seconds=LIMITS["acceptance"]["readonly_shutdown_seconds"] if saved_only else None,
    )
    if saved_only:
        # Saved-result inspection submits no generation and needs only a shutdown interval.
        send = stop - LIMITS["acceptance"]["readonly_shutdown_seconds"]
    budget.configure_deadlines(api.workspace, send, stop)
    api.close()
    api = Client(api.workspace)
    atomic_json(output / "run.json", report)
    try:
        status = api.runtime.start(backend="colab", models=["wai-illustrious"], gpu="T4")
        report["gpu"]["started"] = status.gpu
        if not saved_only:
            if not conversation_only:
                character = api.characters.create(
                    CharacterInput(
                        name="Mira", age="32", appearance="Short silver hair, black apron"
                    )
                )
                story = api.stories.create(
                    StoryInput(name="Bookshop", world="A quiet harbor bookshop")
                )
                requests = [
                    CreationRequest(
                        operation_id=uuid4(),
                        kind="portrait",
                        target_id=character.id,
                        expected_revision=character.revision,
                        locale="en",
                        allow_real_llm=True,
                    ),
                    CreationRequest(
                        operation_id=uuid4(),
                        kind="banner",
                        target_id=story.id,
                        expected_revision=story.revision,
                        locale="en",
                        allow_real_llm=True,
                    ),
                    CreationRequest(
                        operation_id=uuid4(),
                        kind="conversation_image",
                        target_id=identities[0],
                        expected_revision=api.conversations.overview(identities[0]).revision,
                        locale="ja",
                        allow_real_llm=True,
                    ),
                ]
            else:
                subjects_to_check: tuple[Literal["character", "pair"], ...] = ("character", "pair")
                requests = [
                    CreationRequest(
                        operation_id=uuid4(),
                        kind="conversation_image",
                        target_id=identities[0],
                        expected_revision=api.conversations.overview(identities[0]).revision,
                        subjects=subjects,
                        viewpoint="observer",
                        locale="ja",
                        allow_real_llm=True,
                    )
                    for subjects in subjects_to_check
                ]
            for request in requests:
                budget.reserve(api.workspace, str(request.operation_id))
                api.library.accept(request)
                job = api.library.execute(request.operation_id)
                if job.image_id is None:
                    raise KomaquillError(code=job.error or "generation_failed")
                save_image(api, job.image_id, request.kind, output, report)
            if not conversation_only:
                budget.reserve(api.workspace, str(uuid4()))
                image = api.images.submit(
                    "A quiet harbor at sunrise, watercolor", candidates=1, seed=101
                )
                save_image(api, image.run_id, "standalone", output, report)
        report["gpu"]["standard_views"] = api.runtime.views()
        observe(api, output, report, identities)
    except BaseException as error:
        report["gpu"]["failure"] = {
            "type": type(error).__name__,
            "code": getattr(error, "code", None),
        }
        raise
    finally:
        # The workspace was empty before this explicit start; stop only its owned allocation.
        try:
            stopped = api.runtime.stop()
            account = api.runtime.usage()
            confirmed = (
                stopped.gpu in {"stopped", "absent"}
                and not stopped.collector_running
                and account.assignments_count == 0
            )
            report["gpu"].update(
                stopped=stopped.gpu,
                collector_running=stopped.collector_running,
                final_usage=account.model_dump(mode="json"),
                confirmed=confirmed,
            )
            if not confirmed:
                raise KomaquillError(code="shutdown_timeout")
            budget.checkpoint(
                api.workspace, allocation, stopped_at=datetime.now(UTC), released=True
            )
        except Exception as error:
            report["gpu"]["cleanup_failure"] = {
                "type": type(error).__name__,
                "code": getattr(error, "code", None),
            }
            raise
        finally:
            atomic_json(output / "run.json", report)
            api.close()


def observe(api: Client, output: Path, report: dict[str, Any], identities: list[UUID]) -> None:
    """Read saved results and standard views without submitting further work."""
    browse(
        api,
        output,
        report,
        {
            "format_version": 1,
            "real_llm": True,
            "conversations": [str(value) for value in identities],
            "conversation_turn_counts": {
                str(value): api.conversations.overview(value).turn_count for value in identities
            },
            "has_images": any(run.state == "saved" and run.images for run in api.images.list()),
            "standard_views": report.get("gpu", {}).get("standard_views", []),
        },
    )


def browse(
    api: Client,
    output: Path,
    report: dict[str, Any],
    scenario: dict[str, Any],
    *,
    script: str = "scripts/acceptance/web.mjs",
    timeout: float = 240,
) -> None:
    """Own one isolated application and one explicitly selected browser scenario."""
    python = prepare()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LANGCHAIN_", "LANGSMITH_", "KOMAQUILL_ALLOW_"))
    }
    endpoint_file = api.workspace / ".artifacts/state/web-listener.json"
    endpoint_file.unlink(missing_ok=True)
    with (
        (output / "web-server.log").open("w", encoding="utf-8") as log,
        owned_process(
            [str(python), "-m", "komaquill", "--project", str(api.workspace), "web", "--port", "0"],
            cwd=ROOT,
            env=environment,
            stdout=log,
        ) as server,
    ):
        deadline = time.monotonic() + 45
        while not endpoint_file.is_file():
            if server.poll() is not None or time.monotonic() >= deadline:
                raise KomaquillError(code="operation_failed")
            time.sleep(0.1)
        config = output / "browser-input.json"
        atomic_json(
            config,
            {
                "endpoint": json.loads(endpoint_file.read_bytes())["endpoint"],
                **scenario,
            },
        )
        result = run_owned(
            ["node", script, str(config)],
            cwd=ROOT,
            env=environment,
            stdout=log,
            timeout=timeout,
        )
        if result:
            raise KomaquillError(code="operation_failed")
        report["browser"] = {"status": "passed", "manifest": "browser/scenario.json"}


def save_image(
    api: Client, identity: UUID, kind: str, output: Path, report: dict[str, Any]
) -> None:
    run = api.images.wait(identity)
    report["cases"].append({"case": kind, "run_id": str(identity), "state": run.state})
    atomic_json(output / "run.json", report)
    assert run.state == "saved" and run.images
    assert sum(len(value.images) for value in api.images.list()) <= LIMITS["gpu"]["output_limit"]
    for name in run.images:
        assert api.artifact_path(f"runs/{identity}/{name}").is_file()
    reopened = Client(api.workspace)
    try:
        assert reopened.images.get(identity).state == "saved"
    finally:
        reopened.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=message("acceptance.help"))
    parser.add_argument("--allow-real-llm", action="store_true", required=True)
    parser.add_argument("--ui", action="store_true", help=message("acceptance.ui_help"))
    parser.add_argument("--allow-gpu", action="store_true")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--conversation-id", type=UUID)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-seconds", type=int)
    parser.add_argument("--extend-gpu-seconds", type=int)
    parser.add_argument("--authorization-id", type=UUID)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.ui and (args.resume or args.conversation_id or args.allow_gpu):
        parser.error(message("acceptance.ui_conflict"))
    if (args.extend_gpu_seconds is not None or args.authorization_id is not None) and not (
        (args.resume or args.conversation_id)
        and args.allow_gpu
        and args.authorization_id
        and args.extend_gpu_seconds is not None
        and args.extend_gpu_seconds > 0
    ):
        parser.error(message("acceptance.extension_required"))
    time_extension = None
    if args.authorization_id is not None and args.extend_gpu_seconds is not None:
        time_extension = (args.authorization_id, args.extend_gpu_seconds)
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / ".artifacts").resolve()):
        parser.error(message("review.workspace_required"))
    output.mkdir(parents=True, exist_ok=False)
    workspace = output.parent / "workspace"
    report: dict[str, Any] = {
        "format_version": 1,
        **owner(),
        "started_at": datetime.now(UTC).isoformat(),
        "source_revision": source_revision(ROOT),
        "input_sha256": input_fingerprint(ROOT),
        "status": "running",
        "cases": [],
        "real_llm": True,
        "real_gpu": args.allow_gpu,
        "quality": "not_evaluated",
    }
    atomic_json(output / "run.json", report)
    api = None
    opening_calls: int | None = None
    try:
        if not workspace.exists():
            initialized = Client.initialize(workspace)
            (workspace / "komaquill.toml").write_text(
                Path(__file__).with_name("workspace.toml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            initialized.close()
        api = Client(workspace)
        limits = LIMITS["ui" if args.ui else "llm"]
        max_calls = limits["max_calls"] if args.max_calls is None else args.max_calls
        max_seconds = limits["max_seconds"] if args.max_seconds is None else args.max_seconds
        attach(api, output, ledger=args.ledger, max_calls=max_calls, max_seconds=max_seconds)
        opening_value = api.llm_usage()["reserved_calls"]
        assert isinstance(opening_value, int)
        opening_calls = opening_value
        if args.ui:
            browse(
                api,
                output,
                report,
                {"format_version": 1, "real_llm": True},
                script="scripts/acceptance/ui.mjs",
                timeout=max_seconds,
            )
            report["status"] = "passed"
        elif args.conversation_id:
            api.conversations.overview(args.conversation_id)
            identities = [args.conversation_id]
        elif args.resume:
            previous = args.resume.resolve()
            if previous.parent.parent != output.parent or previous.name != "run.json":
                raise KomaquillError(code="invalid_request")
            parent = json.loads(previous.read_bytes())
            report["resumed_from"] = str(previous)
            identities = [UUID(value) for value in parent.get("conversation_ids", [])] or list(
                dict.fromkeys(
                    api.operations.get(UUID(case["operation_id"])).conversation_id
                    for case in parent["cases"]
                    if case.get("state") == "complete" and case.get("operation_id")
                )
            )
            if len(identities) != 2 or len(set(identities)) != 2:
                raise KomaquillError(code="invalid_request")
        else:
            api, identities = conversations(api, output, report)
        if not args.ui:
            report["conversation_ids"] = [str(value) for value in identities]
            before_calls = api.llm_usage()["reserved_calls"]
            if args.allow_gpu:
                gpu(
                    api,
                    output,
                    report,
                    identities,
                    saved_only=bool(args.resume),
                    time_extension=time_extension,
                    conversation_only=args.conversation_id is not None,
                )
            else:
                observe(api, output, report, identities)
            if args.resume:
                assert api.llm_usage()["reserved_calls"] == before_calls
            report["status"] = "passed"
    except BaseException as error:
        report.update(
            status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
            failure=describe_failure(error, "operation").model_dump(mode="json"),
        )
    finally:
        report.update(
            finished_at=datetime.now(UTC).isoformat(),
            budget=None,
            inputs_unchanged=input_fingerprint(ROOT) == report["input_sha256"],
        )
        if api is not None:
            try:
                report["budget"] = api.llm_usage()
            except Exception as error:
                report.update(
                    status="failed",
                    budget_failure=describe_failure(error, "storage").model_dump(mode="json"),
                )
            try:
                api.close()
            except Exception as error:
                report.update(
                    status="failed",
                    cleanup_failure=describe_failure(error, "operation").model_dump(mode="json"),
                )
        report["new_llm_calls"] = (
            report["budget"]["reserved_calls"] - opening_calls
            if opening_calls is not None and report["budget"] is not None
            else None
        )
        atomic_json(output / "run.json", report)
    print(message("review.evidence_saved", path=output / "run.json"))
    return 0 if report["status"] == "passed" and report["inputs_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
