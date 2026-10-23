"""生成・履歴照合・回収をCLIと画面で共通に行う。"""

import hashlib
import io
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from filelock import FileLock
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from komaquill.client import ComfyClient
from komaquill.errors import KomaquillError, SubmissionUnknown
from komaquill.events import failure_info
from komaquill.models import FailureInfo, ImageMetadata, ImageRef, JsonObject, Run, Session
from komaquill.provenance import code_sha256, source_revision
from komaquill.settings import Project
from komaquill.storage import atomic_bytes, atomic_json, load_runs, run_dir, save_run, stop_request
from komaquill.workflow import digest, prepare


@dataclass(frozen=True)
class Collection:
    """回収結果。pendingには受付不明も含む。"""

    saved: int
    pending: int
    failed: int
    errors: tuple[UUID | str, ...] = ()
    pending_ids: tuple[UUID, ...] = ()
    failure: FailureInfo | None = None


def new_run(project: Project, session: Session, run_id: UUID, graph: JsonObject) -> Run:
    """再実行に必要な条件を保存するための履歴を作る。"""
    return Run(
        run_id=run_id,
        runtime_id=session.runtime_id,
        app_version=version("komaquill"),
        source_revision=source_revision(),
        code_sha256=code_sha256(),
        remote_code_sha256=session.code_sha256,
        remote_source_revision=session.source_revision,
        comfy_revision=session.comfy_revision,
        frontend_version=session.frontend_version,
        model_sha256=session.model_sha256,
        workflow_sha256=digest(graph),
    )


def request_id(prompt: list[Any]) -> UUID | None:
    """ComfyUIが保持するextra_dataからローカル実行IDを読む。"""
    try:
        return UUID(str(prompt[3]["komaquill"]["run_id"]))
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def save_images(project: Project, client: ComfyClient, run: Run, record: JsonObject) -> list[str]:
    """Verify each image and persist its integrity information before continuing."""
    image_names: list[str] = []
    outputs = record.get("outputs", {})
    if not isinstance(outputs, dict):
        raise KomaquillError("画像の出力形式を確認できません。", code="invalid_output")
    for output in outputs.values():
        if not isinstance(output, dict) or not isinstance(output.get("images", []), list):
            raise KomaquillError("画像の出力形式を確認できません。", code="invalid_output")
        for raw_reference in output.get("images", []):
            reference = ImageRef.model_validate(raw_reference)
            suffix = PurePosixPath(reference.filename).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}:
                raise KomaquillError("未対応の画像形式です。", code="unsupported_image")
            name = f"images/{len(image_names) + 1:04d}{suffix}"
            target = run_dir(project, run.run_id) / name
            previous = run.image_metadata.get(name)
            content = target.read_bytes() if target.is_file() else None
            if (
                content is None
                or previous is None
                or (hashlib.sha256(content).hexdigest() != previous.sha256)
            ):
                content = client.image(reference)
            try:
                with Image.open(io.BytesIO(content)) as image:
                    width, height = image.size
                    image.verify()
            except (UnidentifiedImageError, OSError, SyntaxError) as error:
                raise KomaquillError(
                    "画像を検証できません。collect で再回収してください。",
                    code="image_invalid",
                    stage="image_validation",
                ) from error
            if not target.is_file() or target.read_bytes() != content:
                atomic_bytes(target, content)
            run.image_metadata[name] = ImageMetadata(
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                width=width,
                height=height,
                source=reference,
            )
            image_names.append(name)
            run.images = list(image_names)
            save_run(project, run)
    if not image_names:
        raise KomaquillError(
            "保存する画像がありません。ワークフローの出力を確認してください。", code="missing_image"
        )
    return image_names


def generation_times(run: Run, status: JsonObject) -> None:
    """Record server timestamps only when ComfyUI reports valid execution events."""
    messages = status.get("messages", [])
    if not isinstance(messages, list):
        return
    for row in messages:
        if not isinstance(row, (list, tuple)) or len(row) != 2 or not isinstance(row[1], dict):
            continue
        value = row[1].get("timestamp")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            timestamp = datetime.fromtimestamp(value / 1000, UTC)
        except (OverflowError, OSError, ValueError):
            continue
        if row[0] == "execution_start":
            run.generation_started_at = timestamp
        elif row[0] in {"execution_success", "execution_error", "execution_interrupted"}:
            run.generation_completed_at = timestamp


def verify_runtime(client: ComfyClient, session: Session) -> None:
    """管理する環境だけを識別ファイルで照合する。"""
    if session.backend == "colab" and str(client.runtime().get("runtime_id")) != str(
        session.runtime_id
    ):
        raise KomaquillError("ランタイムが変わりました。start で接続し直してください。")


def collection_lock(project: Project) -> FileLock:
    """生成要求と回収結果を同じ排他制御で更新する。"""
    project.runs.mkdir(parents=True, exist_ok=True)
    return FileLock(str(project.runs / ".collection.lock"))


def save_conditions(project: Project, run: Run, prompt: list[Any]) -> None:
    """観測した生成条件を、完了を待たず保存する。"""
    folder = run_dir(project, run.run_id)
    graph = prompt[2]
    if not isinstance(graph, dict):
        raise KomaquillError("生成条件の形式を確認できません。")
    if not (folder / "workflow-api.json").is_file():
        atomic_json(folder / "workflow-api.json", graph)
    graph_digest = digest(graph)
    if run.workflow_sha256 != graph_digest:
        run.workflow_sha256 = graph_digest
        save_run(project, run)
    extra = prompt[3] if len(prompt) > 3 else {}
    if isinstance(extra, dict):
        metadata = extra.get("extra_pnginfo", {})
        ui = metadata.get("workflow") if isinstance(metadata, dict) else None
        if isinstance(ui, dict) and not (folder / "workflow.json").is_file():
            atomic_json(folder / "workflow.json", ui)


def collect(
    project: Project, client: ComfyClient, session: Session, only: UUID | None = None
) -> Collection:
    """キューと履歴を記録し、要求ごとに画像を回収する。"""
    project = project.configured(session.settings)
    with collection_lock(project):
        verify_runtime(client, session)
        records: dict[str, JsonObject] = {row[1]: {"prompt": row} for row in client.queue()}
        records.update(client.history())
        unreadable: list[UUID | str] = []
        runs = {run.run_id: run for run in load_runs(project, unreadable=unreadable)}
        known_prompts = {
            run.prompt_id: run.run_id
            for run in runs.values()
            if run.runtime_id == session.runtime_id and run.prompt_id is not None
        }
        if only is not None and only not in runs and only not in unreadable:
            raise KomaquillError("指定した生成履歴が見つかりません。")
        related: set[UUID] = set()
        errors = [identity for identity in unreadable if only is None or identity == only]
        saved = 0
        for prompt_id, raw_record in records.items():
            record = raw_record if isinstance(raw_record, dict) else {}
            raw_prompt = record.get("prompt")
            prompt = raw_prompt if isinstance(raw_prompt, list) else []
            identity = (
                request_id(prompt)
                or known_prompts.get(prompt_id)
                or uuid5(NAMESPACE_URL, f"komaquill:{session.runtime_id}:{prompt_id}")
            )
            if identity in unreadable or (only is not None and identity != only):
                continue
            run = runs.get(identity)
            if run is None:
                if prompt_id in session.excluded_prompt_ids:
                    continue
                graph = prompt[2] if len(prompt) > 2 and isinstance(prompt[2], dict) else {}
                run = new_run(project, session, identity, graph)
                runs[identity] = run
            owns_request = session.backend == "external" and request_id(prompt) == run.run_id
            if run.runtime_id != session.runtime_id and not owns_request:
                continue
            related.add(run.run_id)
            if not run.pending:
                continue
            try:
                run.prompt_id = prompt_id
                run.state = "queued"
                save_run(project, run)
                if not isinstance(prompt, list) or len(prompt) < 3:
                    raise KomaquillError("生成条件の形式を確認できません。")
                save_conditions(project, run, prompt)
                status = record.get("status", {})
                if not isinstance(status, dict):
                    raise KomaquillError("生成状態の形式を確認できません。")
                if status.get("status_str") == "error":
                    run.state = "failed"
                    run.error = "ComfyUIで生成に失敗しました。実行ログを確認してください。"
                    run.failure = failure_info(
                        KomaquillError("", code="generation_failed", retryable=False), "generation"
                    )
                    generation_times(run, status)
                    save_run(project, run)
                    continue
                generation_times(run, status)
                if not status.get("completed"):
                    save_run(project, run)
                    continue
                run.images = save_images(project, client, run, record)
                run.state = "saved"
                run.error = None
                run.failure = None
                save_run(project, run)
                saved += 1
            except (KomaquillError, OSError, ValidationError) as error:
                errors.append(run.run_id)
                run.state = "queued"
                stage = "storage"
                if isinstance(error, ValidationError):
                    stage = "history_validation"
                elif isinstance(error, KomaquillError):
                    stage = error.stage if error.stage != "operation" else "image_collection"
                run.failure = failure_info(error, stage)
                run.error = "画像や生成条件を保存できません。collect で再回収してください。"
                with suppress(OSError):
                    save_run(project, run)
        selected = [
            run
            for run in runs.values()
            if (run.runtime_id == session.runtime_id or run.run_id in related)
            and (only is None or run.run_id == only)
        ]
        pending = tuple(run.run_id for run in selected if run.pending)
        return Collection(
            saved=saved,
            pending=len(pending),
            failed=sum(run.state == "failed" for run in selected),
            errors=tuple(errors),
            pending_ids=pending,
        )


def mark_lost(project: Project, runtime_id: UUID) -> tuple[UUID, ...]:
    """終了を確認したColabの未回収要求を確定する。"""
    lost: list[UUID] = []
    with collection_lock(project):
        unreadable: list[UUID | str] = []
        for run in load_runs(project, unreadable=unreadable):
            if run.runtime_id == runtime_id and run.pending:
                run.state = "lost"
                run.error = "Colabの終了により画像を回収できません。"
                run.failure = failure_info(
                    KomaquillError("", code="runtime_ended", retryable=False), "collection"
                )
                save_run(project, run)
                lost.append(run.run_id)
        if unreadable:
            raise KomaquillError("生成履歴を読めません: " + ", ".join(map(str, unreadable)))
    return tuple(lost)


def generate(
    project: Project,
    client: ComfyClient,
    session: Session,
    prompt: str,
    negative: str,
    seed: int,
) -> Run:
    """送信前に記録し、ローカル保存が終わるまで待つ。"""
    project = project.configured(session.settings)
    verify_runtime(client, session)
    run_id = uuid4()
    graph, ui = prepare(prompt, negative, seed, str(run_id))
    run = new_run(project, session, run_id, graph)
    folder = run_dir(project, run_id)
    run.state = "unknown"
    with collection_lock(project):
        if stop_request(project, session) is not None:
            raise KomaquillError("終了処理中です。start で接続してから生成してください。")
        atomic_json(folder / "workflow-api.json", graph)
        atomic_json(folder / "workflow.json", ui)
        save_run(project, run)
    try:
        prompt_id = client.submit(graph, ui, str(run_id))
    except SubmissionUnknown as exc:
        raise SubmissionUnknown(
            f"受付結果が不明です。collect {run_id} で照合してください。"
        ) from exc
    except KomaquillError as error:
        with collection_lock(project):
            run = Run.model_validate_json((folder / "run.json").read_bytes())
            if run.pending:
                run.state = "failed"
                run.error = "生成要求が受け付けられませんでした。"
                run.failure = failure_info(error, "submission")
                save_run(project, run)
        raise
    with collection_lock(project):
        run = Run.model_validate_json((folder / "run.json").read_bytes())
        run.prompt_id = prompt_id
        if run.pending:
            run.state = "queued"
        save_run(project, run)
    settings = project.settings()
    deadline = time.monotonic() + settings.generation_timeout
    while time.monotonic() < deadline:
        result = collect(project, client, session, only=run_id)
        run = Run.model_validate_json((folder / "run.json").read_bytes())
        if run.state == "saved":
            return run
        if run.state in {"failed", "lost"}:
            raise KomaquillError(run.error or "生成に失敗しました。")
        if run_id in result.errors:
            raise KomaquillError(
                f"画像や生成条件を保存できません。collect {run_id} で再回収してください。"
            )
        time.sleep(settings.poll_interval)
    raise KomaquillError(f"待機時間を超えました。collect {run_id} で回収してください。")
