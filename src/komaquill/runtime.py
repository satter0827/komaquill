"""接続の維持、自動回収と終了を行う。"""

import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import keyring
from filelock import FileLock, Timeout

from komaquill.client import ComfyClient
from komaquill.colab import Colab, check_port, terminate
from komaquill.downloads import fetch_model
from komaquill.errors import KomaquillError
from komaquill.models import Session
from komaquill.service import collect, request_id, source_revision, verify_runtime
from komaquill.settings import Project
from komaquill.storage import atomic_json, load_runs, load_session
from komaquill.workflow import preset


def credential_key(project: Project, session: Session) -> str:
    """接続先と利用者を資格情報の保存先に含める。"""
    return f"{project.root}|{session.endpoint}|{session.username}"


@contextmanager
def connected(project: Project) -> Iterator[tuple[ComfyClient, Session]]:
    """保存した接続先を使い、GPUを新しく確保しない。"""
    if not project.session_file.is_file():
        raise KomaquillError("接続先が未設定です。start を実行してください。")
    session = load_session(project)
    password = None
    if session.username is not None:
        password = keyring.get_password("komaquill", credential_key(project, session))
        if password is None:
            raise KomaquillError("資格情報がありません。start で接続し直してください。")
    client = ComfyClient(
        session.endpoint, password, project.settings().request_timeout, username=session.username
    )
    try:
        yield client, session
    finally:
        client.close()


def monitor_lock(project: Project) -> FileLock:
    """二重起動と終了中の再起動を防ぐ。"""
    project.runtime.mkdir(parents=True, exist_ok=True)
    return FileLock(str(project.runtime / ".monitor.lock"))


def excluded_history(client: ComfyClient) -> set[str]:
    """接続前のキューと履歴を回収対象から外す。"""
    queue = client.json("/queue")
    excluded = {
        str(row[1])
        for rows in queue.values()
        if isinstance(rows, list)
        for row in rows
        if len(row) > 1
    }
    excluded.update(client.history())
    return excluded


def pending_ids(project: Project, client: ComfyClient, session: Session) -> set[str]:
    """未完了・未回収のIDをCLIと画面の両方から集める。"""
    runs = {run.run_id: run for run in load_runs(project)}
    pending = {
        str(run.run_id)
        for run in runs.values()
        if run.runtime_id == session.runtime_id and run.state not in {"saved", "failed"}
    }
    queue = client.json("/queue")
    for rows in queue.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if len(row) < 2:
                continue
            identity = request_id(row)
            own = identity in runs and runs[identity].state not in {"saved", "failed"}
            if own:
                pending.add(str(identity))
            elif str(row[1]) not in session.excluded_prompt_ids:
                pending.add(str(row[1]))
    return pending


def stop_request(project: Project, session: Session) -> bool | None:
    """同じ接続に対する終了要求だけを読む。"""
    path = project.runtime / "stop.json"
    if not path.is_file():
        return None
    import json

    request = json.loads(path.read_text(encoding="utf-8"))
    if request.get("runtime_id") != str(session.runtime_id):
        return None
    return bool(request.get("force"))


def drain(
    project: Project,
    client: ComfyClient,
    session: Session,
    force: bool,
    echo: Callable[[str], None],
) -> None:
    """上限まで生成を待ち、取得可能な結果を保存する。"""
    deadline = time.monotonic() + project.settings().shutdown_timeout
    remaining: set[str] = set()
    while True:
        try:
            try:
                result = collect(project, client, session)
                if result.saved:
                    echo(f"保存しました: {result.saved}件")
                remaining = pending_ids(project, client, session)
            except KomaquillError:
                remaining.update(
                    str(run.run_id)
                    for run in load_runs(project)
                    if run.runtime_id == session.runtime_id and run.state not in {"saved", "failed"}
                )
                echo("接続できないため回収を完了できませんでした。未回収の管理記録を残します。")
                break
            if (
                force
                or stop_request(project, session) is True
                or not remaining
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(project.settings().poll_interval)
        except KeyboardInterrupt:
            force = True
    if remaining:
        echo("未完了・未回収: " + ", ".join(sorted(remaining)))


def watch(
    project: Project,
    client: ComfyClient,
    session: Session,
    colab: Colab | None,
    echo: Callable[[str], None],
    tunnel: subprocess.Popen[bytes] | None = None,
) -> None:
    """startの端末で接続と回収を維持する。"""
    disconnected = False
    force = False
    try:
        while stop_request(project, session) is None:
            try:
                if colab is not None and tunnel is not None and tunnel.poll() is not None:
                    tunnel = None
                if colab is not None and tunnel is None:
                    receipt = colab.receipt()
                    if receipt is None:
                        raise KomaquillError("Colabの所有記録がありません。")
                    tunnel = colab.tunnel(receipt)
                result = collect(project, client, session)
                if disconnected:
                    echo("再接続しました。")
                if result.saved:
                    echo(f"保存しました: {result.saved}件")
                disconnected = False
            except KomaquillError:
                if not disconnected:
                    echo("接続を確認しています。未回収の結果は次の接続時に回収します。")
                disconnected = True
            time.sleep(project.settings().poll_interval)
        force = stop_request(project, session) is True
    except KeyboardInterrupt:
        echo("生成と回収を待っています。すぐ終了するには、もう一度Ctrl+Cを押してください。")
    finally:
        try:
            drain(project, client, session, force, echo)
        finally:
            if tunnel is not None:
                terminate(tunnel, project.settings().control_timeout)


def start_external(
    project: Project,
    endpoint: str,
    username: str | None,
    password: str | None,
    echo: Callable[[str], None],
) -> None:
    """起動済みComfyUIへの接続だけを維持する。"""
    session = Session(endpoint=endpoint, backend="external", runtime_id=uuid4(), username=username)
    if project.session_file.is_file():
        prior = load_session(project)
        if prior.backend == "external" and prior.endpoint == session.endpoint:
            session = prior.model_copy(update={"username": username})
    client = ComfyClient(
        session.endpoint, password, project.settings().request_timeout, username=username
    )
    try:
        client.json("/system_stats")
        if (
            not project.session_file.is_file()
            or load_session(project).runtime_id != session.runtime_id
        ):
            session.excluded_prompt_ids = excluded_history(client)
        if username is not None and password is not None:
            keyring.set_password("komaquill", credential_key(project, session), password)
        atomic_json(project.session_file, session.model_dump(mode="json"))
        echo("接続しました。この端末を開いたまま generate または open を実行してください。")
        watch(project, client, session, None, echo)
    finally:
        client.close()


def start_colab(project: Project, echo: Callable[[str], None]) -> None:
    """確保から終了確認まで一つのColabを所有する。"""
    colab = Colab(project)
    process = None
    tunnel = None
    cleanup_owned = False
    try:
        check_port(project.settings().local_port)
        echo("モデルを取得・検証しています。")
        fetch_model(project.artifacts, preset())
        cleanup_owned = colab.receipt() is None
        receipt, created = colab.acquire(source_revision(project))
        cleanup_owned = True
        if created:
            echo("Colabで実行環境とモデルを準備しています。")
            process = colab.prepare(receipt)
        tunnel = colab.tunnel(receipt)
        session = Session(
            endpoint=f"http://127.0.0.1:{project.settings().local_port}",
            runtime_id=receipt.runtime_id,
        )
        client = ComfyClient(session.endpoint, timeout=project.settings().request_timeout)
        try:
            deadline = time.monotonic() + project.settings().preparation_timeout
            while True:
                try:
                    metadata = client.runtime()
                    session = Session.model_validate({**metadata, "endpoint": session.endpoint})
                    if session.runtime_id != receipt.runtime_id:
                        raise KomaquillError("ColabのランタイムIDが所有記録と異なります。")
                    verify_runtime(client, session)
                    break
                except KomaquillError:
                    if (
                        time.monotonic() >= deadline
                        or (process is not None and process.poll() is not None)
                        or tunnel.poll() is not None
                    ):
                        raise KomaquillError(
                            "ComfyUIを起動できませんでした。準備状況を確認してください。"
                        ) from None
                    time.sleep(project.settings().poll_interval)
            atomic_json(project.session_file, session.model_dump(mode="json"))
            echo("接続しました。この端末を開いたまま generate または open を実行してください。")
            watch(project, client, session, colab, echo, tunnel)
        finally:
            client.close()
    finally:
        if tunnel is not None and tunnel.poll() is None:
            terminate(tunnel, project.settings().control_timeout)
        if process is not None and process.poll() is None:
            terminate(process, project.settings().control_timeout)
        if cleanup_owned:
            final_receipt = colab.receipt()
            try:
                if final_receipt is not None:
                    colab.recover_diagnostics(final_receipt)
            finally:
                colab.stop()


def start(
    project: Project,
    backend: str,
    endpoint: str | None,
    username: str | None,
    password: str | None,
    echo: Callable[[str], None],
) -> None:
    """二重起動を拒否し、明示された接続先を開始する。"""
    try:
        with monitor_lock(project).acquire(timeout=0):
            (project.runtime / "stop.json").unlink(missing_ok=True)
            if backend == "external":
                if endpoint is None:
                    raise KomaquillError("外部接続には --endpoint を指定してください。")
                if Colab(project).receipt() is not None:
                    raise KomaquillError(
                        "前回のColabが残っています。stop で終了してから接続してください。"
                    )
                start_external(project, endpoint, username, password, echo)
            else:
                if endpoint is not None or username is not None:
                    raise KomaquillError("Colab接続にURLやBasic認証は不要です。")
                start_colab(project, echo)
    except Timeout:
        raise KomaquillError(
            "すでに start が実行中です。利用後は stop を実行してください。"
        ) from None


def stop(project: Project, force: bool, echo: Callable[[str], None]) -> None:
    """監視中は終了を依頼し、監視がなければ残存Colabだけを終了する。"""
    lock = monitor_lock(project)
    had_monitor = False
    expected_runtime: UUID | None = None
    try:
        lock.acquire(timeout=0)
    except Timeout:
        had_monitor = True
        if not project.session_file.is_file():
            raise KomaquillError("起動処理中です。start の端末でCtrl+Cを押してください。") from None
        session = load_session(project)
        atomic_json(
            project.runtime / "stop.json", {"runtime_id": str(session.runtime_id), "force": force}
        )
        echo("終了を依頼しました。生成と回収の終了を待っています。")
        try:
            lock.acquire(
                timeout=project.settings().shutdown_timeout + project.settings().control_timeout
            )
        except Timeout:
            raise KomaquillError("終了処理を待機中です。status で確認してください。") from None
    try:
        colab = Colab(project)
        receipt = colab.receipt()
        if (
            expected_runtime is not None
            and receipt is not None
            and receipt.runtime_id != expected_runtime
        ):
            raise KomaquillError(
                "接続が変わったため終了対象を確認できません。status で確認してください。"
            )
        if not had_monitor and project.session_file.is_file():
            session = load_session(project)
            if session.backend == "external" or (
                receipt is not None and receipt.runtime_id == session.runtime_id
            ):
                try:
                    with connected(project) as (client, session):
                        drain(project, client, session, force, echo)
                except KomaquillError:
                    echo("未回収の結果があります。管理記録を確認してください。")
        colab.stop()
        echo("終了しました。")
    finally:
        lock.release()
