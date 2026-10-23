"""生成要求と回収結果を原子的に保存する。"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from komaquill.errors import KomaquillError
from komaquill.events import record_event
from komaquill.models import JsonObject, Run, Session
from komaquill.settings import Project


def atomic_bytes(path: Path, content: bytes) -> None:
    """同じフォルダーの一時ファイルから置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, data: JsonObject) -> None:
    """UTF-8でJSONを保存する。"""
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def run_dir(project: Project, run_id: UUID) -> Path:
    """UUIDを使い遠隔ファイル名を保存パスに使わない。"""
    return project.runs / str(run_id)


def save_run(project: Project, run: Run) -> None:
    """生成状態を更新する。"""
    path = run_dir(project, run.run_id) / "run.json"
    previous = Run.model_validate_json(path.read_bytes()) if path.is_file() else None
    if previous == run:
        return
    run.updated_at = datetime.now(UTC)
    if run.state == "saved" and run.saved_at is None:
        run.saved_at = run.updated_at
    atomic_json(path, run.model_dump(mode="json"))
    if previous is None or previous.state != run.state or previous.failure != run.failure:
        record_event(
            project,
            "generation",
            run.state,
            runtime_id=run.runtime_id,
            run_id=run.run_id,
            failure=run.failure,
        )


def load_runs(project: Project, *, unreadable: list[UUID | str] | None = None) -> list[Run]:
    """履歴を読む。unreadableの指定時は読めないIDを報告して継続する。"""
    runs: list[Run] = []
    for path in project.runs.glob("*/run.json"):
        try:
            identity = UUID(path.parent.name)
        except ValueError:
            if unreadable is None:
                raise KomaquillError("生成履歴のフォルダー名を確認してください。") from None
            unreadable.append(f"runs/{path.parent.name!r}")
            continue
        try:
            run = Run.model_validate_json(path.read_bytes())
            if run.run_id != identity:
                raise ValueError("履歴とフォルダーのIDが一致しません。")
        except (OSError, ValueError):
            if unreadable is None:
                raise KomaquillError(
                    f"生成履歴を読めません。ファイルとアクセス権を確認してください: {identity}"
                ) from None
            unreadable.append(identity)
            continue
        runs.append(run)
    return runs


def load_session(project: Project) -> Session:
    """保存済み接続先を読み込む。"""
    return Session.model_validate_json(project.session_file.read_bytes())


def stop_request(project: Project, session: Session) -> bool | None:
    """同じ接続に対する終了要求だけを読む。"""
    path = project.runtime / "stop.json"
    if not path.is_file():
        return None
    request = json.loads(path.read_text(encoding="utf-8"))
    if request.get("runtime_id") != str(session.runtime_id):
        return None
    return bool(request.get("force"))
