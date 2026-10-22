"""生成・履歴照合・回収をCLIと画面で共通に行う。"""

import io
import subprocess
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import PurePosixPath
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from filelock import FileLock
from PIL import Image, UnidentifiedImageError

from komaquill.client import ComfyClient
from komaquill.errors import KomaquillError, SubmissionUnknown
from komaquill.models import ImageRef, JsonObject, Run, Session
from komaquill.settings import Project
from komaquill.storage import atomic_bytes, atomic_json, load_runs, run_dir, save_run
from komaquill.workflow import digest, prepare


@dataclass(frozen=True)
class Collection:
    """回収結果。pendingには受付不明も含む。"""

    saved: int
    pending: int
    failed: int


def source_revision(project: Project) -> str:
    """実際のローカルソースのコミットを記録する。"""
    result = subprocess.run(
        ["git", "-C", str(project.root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "uncommitted"


def new_run(project: Project, session: Session, run_id: UUID, graph: JsonObject) -> Run:
    """再実行に必要な条件を保存するための履歴を作る。"""
    return Run(
        run_id=run_id,
        runtime_id=session.runtime_id,
        app_version=version("komaquill"),
        source_revision=source_revision(project),
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
    """遠隔パスをローカルパスに使わず画像を検証して保存する。"""
    image_names: list[str] = []
    for output in record.get("outputs", {}).values():
        for raw_reference in output.get("images", []):
            reference = ImageRef.model_validate(raw_reference)
            suffix = PurePosixPath(reference.filename).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}:
                raise KomaquillError("未対応の画像形式です。ComfyUIの出力を確認してください。")
            name = f"images/{len(image_names) + 1:04d}{suffix}"
            target = run_dir(project, run.run_id) / name
            if not target.is_file():
                content = client.image(reference)
                try:
                    with Image.open(io.BytesIO(content)) as image:
                        image.verify()
                except (UnidentifiedImageError, OSError, SyntaxError) as exc:
                    raise KomaquillError(
                        "画像を検証できません。collect で再回収してください。"
                    ) from exc
                atomic_bytes(target, content)
            image_names.append(name)
    if not image_names:
        raise KomaquillError("保存する画像がありません。ワークフローの出力を確認してください。")
    return image_names


def collect(
    project: Project, client: ComfyClient, session: Session, only: UUID | None = None
) -> Collection:
    """生成元を問わず完了した画像を一度だけ保存する。"""
    project.runs.mkdir(parents=True, exist_ok=True)
    with FileLock(str(project.runs / ".collection.lock")):
        if str(client.runtime().get("runtime_id")) != str(session.runtime_id):
            raise KomaquillError("Colabのランタイムが変わりました。start で接続し直してください。")
        history = client.history()
        runs = {run.run_id: run for run in load_runs(project)}
        if only is not None and only not in runs:
            raise KomaquillError("指定した生成履歴が見つかりません。")
        # POST応答が失われた場合も、実行IDでキューと完了履歴を照合する。
        unknown = [
            r for r in runs.values() if r.runtime_id == session.runtime_id and not r.prompt_id
        ]
        if unknown:
            queue = client.json("/queue")
            prompts = list(queue.get("queue_running", [])) + list(queue.get("queue_pending", []))
            prompts += [r.get("prompt", []) for r in history.values()]
            for prompt in prompts:
                identity = request_id(prompt)
                if identity in runs and len(prompt) > 1:
                    runs[identity].prompt_id = str(prompt[1])
                    runs[identity].state = "queued"
                    save_run(project, runs[identity])
        saved = 0
        for prompt_id, record in history.items():
            prompt = record.get("prompt", [])
            identity = request_id(prompt) or uuid5(
                NAMESPACE_URL, f"komaquill:{session.runtime_id}:{prompt_id}"
            )
            if only is not None and identity != only:
                continue
            graph = cast(JsonObject, prompt[2] if len(prompt) > 2 else {})
            run = runs.get(identity)
            if run is None:
                run = new_run(project, session, identity, graph)
                run.prompt_id = prompt_id
                run.state = "queued"
                runs[identity] = run
                save_run(project, run)
            if run.runtime_id != session.runtime_id or run.state == "saved":
                continue
            status = record.get("status", {})
            if status.get("status_str") == "error":
                run.state = "failed"
                run.error = "ComfyUIで生成に失敗しました。画面のエラーを確認してください。"
                save_run(project, run)
                continue
            if not status.get("completed"):
                continue
            folder = run_dir(project, run.run_id)
            atomic_json(folder / "workflow-api.json", graph)
            if len(prompt) > 3:
                ui = prompt[3].get("extra_pnginfo", {}).get("workflow")
                if isinstance(ui, dict):
                    atomic_json(folder / "workflow.json", ui)
            run.images = save_images(project, client, run, record)
            run.state = "saved"
            run.error = None
            save_run(project, run)
            saved += 1
        selected = [
            r
            for r in runs.values()
            if r.runtime_id == session.runtime_id and (only is None or r.run_id == only)
        ]
        return Collection(
            saved=saved,
            pending=sum(r.state not in {"saved", "failed"} for r in selected),
            failed=sum(r.state == "failed" for r in selected),
        )


def generate(
    project: Project,
    client: ComfyClient,
    session: Session,
    prompt: str,
    negative: str,
    seed: int,
) -> Run:
    """送信前に記録し、ローカル保存が終わるまで待つ。"""
    if str(client.runtime().get("runtime_id")) != str(session.runtime_id):
        raise KomaquillError("Colabのランタイムが変わりました。start で接続し直してください。")
    run_id = uuid4()
    graph, ui = prepare(prompt, negative, seed, str(run_id))
    run = new_run(project, session, run_id, graph)
    folder = run_dir(project, run_id)
    atomic_json(folder / "workflow-api.json", graph)
    atomic_json(folder / "workflow.json", ui)
    run.state = "unknown"
    save_run(project, run)
    try:
        run.prompt_id = client.submit(graph, ui, str(run_id))
    except SubmissionUnknown as exc:
        raise SubmissionUnknown(
            f"受付結果が不明です。collect {run_id} で照合してください。"
        ) from exc
    except KomaquillError:
        run.state = "failed"
        run.error = "生成要求が受け付けられませんでした。"
        save_run(project, run)
        raise
    run.state = "queued"
    save_run(project, run)
    settings = project.settings()
    deadline = time.monotonic() + settings.generation_timeout
    while time.monotonic() < deadline:
        collect(project, client, session, only=run_id)
        run = Run.model_validate_json((folder / "run.json").read_bytes())
        if run.state == "saved":
            return run
        if run.state == "failed":
            raise KomaquillError(run.error or "生成に失敗しました。")
        time.sleep(settings.poll_interval)
    raise KomaquillError(f"待機時間を超えました。collect {run_id} で回収してください。")
