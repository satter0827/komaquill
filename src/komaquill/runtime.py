"""接続の維持、自動回収と終了を行う。"""

import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import keyring
from filelock import FileLock, Timeout

from komaquill.client import ComfyClient
from komaquill.colab import Colab, Receipt, check_port, terminate
from komaquill.downloads import fetch_model
from komaquill.errors import KomaquillError
from komaquill.models import Session
from komaquill.service import collect, collection_lock, mark_lost, source_revision, verify_runtime
from komaquill.settings import Project
from komaquill.storage import atomic_json, load_runs, load_session, stop_request
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


def request_stop(project: Project, session: Session, force: bool) -> None:
    """並行する終了要求で強制終了を通常終了へ戻さない。"""
    with collection_lock(project), FileLock(str(project.runtime / ".stop.lock")):
        force = force or stop_request(project, session) is True
        atomic_json(
            project.runtime / "stop.json", {"runtime_id": str(session.runtime_id), "force": force}
        )


def drain(
    project: Project,
    client: ComfyClient,
    session: Session,
    force: bool,
    echo: Callable[[str], None],
) -> None:
    """上限まで生成を待ち、取得可能な結果を保存する。"""
    deadline = time.monotonic() + project.settings().shutdown_timeout
    remaining: tuple[UUID, ...] = ()
    previous_errors: tuple[UUID, ...] = ()
    while True:
        try:
            try:
                result = collect(project, client, session)
                if result.saved:
                    echo(f"保存しました: {result.saved}件")
                if result.errors and result.errors != previous_errors:
                    echo("再回収が必要です: " + ", ".join(map(str, result.errors)))
                previous_errors = result.errors
                remaining = result.pending_ids
            except KomaquillError:
                remaining = tuple(
                    run.run_id
                    for run in load_runs(project)
                    if run.runtime_id == session.runtime_id and run.pending
                )
                echo("接続できないため回収を完了できませんでした。管理記録を保持します。")
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
            request_stop(project, session, True)
    if remaining:
        echo("未完了・未回収: " + ", ".join(map(str, remaining)))


def watch(
    project: Project,
    client: ComfyClient,
    session: Session,
    echo: Callable[[str], None],
    reconnect: Callable[[], None] | None = None,
) -> bool:
    """接続と回収を維持し、終了時に待機方法を返す。"""
    disconnected = False
    previous_errors: tuple[UUID, ...] = ()
    try:
        while stop_request(project, session) is None:
            try:
                if reconnect is not None:
                    reconnect()
                result = collect(project, client, session)
                if disconnected:
                    echo("再接続しました。")
                if result.saved:
                    echo(f"保存しました: {result.saved}件")
                if result.errors and result.errors != previous_errors:
                    echo("再回収が必要です: " + ", ".join(map(str, result.errors)))
                previous_errors = result.errors
                disconnected = False
            except KomaquillError:
                if not disconnected:
                    echo("接続を確認しています。未回収の結果は次の接続時に回収します。")
                disconnected = True
            time.sleep(project.settings().poll_interval)
        return stop_request(project, session) is True
    except KeyboardInterrupt:
        echo("生成と回収を待っています。すぐ終了するには、もう一度Ctrl+Cを押してください。")
        request_stop(project, session, False)
        return False


def finish_colab(
    project: Project,
    colab: Colab,
    receipt: Receipt,
    client: ComfyClient | None,
    session: Session | None,
    force: bool,
    echo: Callable[[str], None],
    processes: list[subprocess.Popen[bytes]],
) -> None:
    """回収から所有記録の削除まで、終了処理の順序を統一する。"""
    confirmed = False
    failures: list[str] = []
    try:
        if client is not None and session is not None:
            try:
                request_stop(project, session, force)
            except OSError:
                failures.append("終了要求を保存できません。")
            try:
                drain(project, client, session, force, echo)
            except (KomaquillError, OSError):
                echo("回収を完了できませんでした。保存先と接続を確認してください。")
        try:
            failed_logs = colab.recover_diagnostics(receipt)
            if failed_logs:
                echo("ログを一部回収できません: " + ", ".join(failed_logs))
        except (KomaquillError, OSError, KeyboardInterrupt):
            echo("ログを回収できませんでした。")
        try:
            try:
                colab.stop()
            except KeyboardInterrupt:
                echo("Colabの終了を再確認しています。")
                colab.stop()
            confirmed = True
        except KomaquillError:
            failures.append("Colabの終了を確認できません。")
    finally:
        for process in processes:
            try:
                terminate(process, project.settings().control_timeout)
            except (OSError, subprocess.SubprocessError, KomaquillError, KeyboardInterrupt):
                failures.append("ローカルの接続処理を終了できません。")
        try:
            colab.cleanup_processes(receipt)
        except (OSError, KomaquillError):
            failures.append("ローカルの実行待ち処理を終了できません。")
    if confirmed:
        lost = mark_lost(project, receipt.runtime_id)
        if lost:
            echo("回収不能: " + ", ".join(map(str, lost)))
        if not failures:
            colab.release(receipt)
    if failures:
        raise KomaquillError(
            " ".join(failures) + " 管理記録を残しました。stop で再確認してください。"
        )


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
        if (
            prior.backend == "external"
            and prior.endpoint == session.endpoint
            and prior.username == username
        ):
            session = prior.model_copy(update={"username": username})
    client = ComfyClient(
        session.endpoint, password, project.settings().request_timeout, username=username
    )
    force = True
    ready = False
    try:
        client.json("/system_stats")
        session.excluded_prompt_ids = excluded_history(client)
        if username is not None and password is not None:
            keyring.set_password("komaquill", credential_key(project, session), password)
        atomic_json(project.session_file, session.model_dump(mode="json"))
        echo("接続しました。この端末を開いたまま generate または open を実行してください。")
        ready = True
        force = watch(project, client, session, echo)
    finally:
        try:
            if ready:
                drain(project, client, session, force, echo)
        finally:
            client.close()


def start_colab(project: Project, echo: Callable[[str], None]) -> None:
    """確保から終了確認まで一つのColabを所有する。"""
    colab = Colab(project)
    processes: list[subprocess.Popen[bytes]] = []
    tunnel: subprocess.Popen[bytes] | None = None
    client: ComfyClient | None = None
    session: Session | None = None
    cleanup_owned = False
    force = True
    try:
        check_port(project.settings().local_port)
        echo("モデルを取得・検証しています。")
        fetch_model(project.artifacts, preset())
        cleanup_owned = colab.receipt() is None
        receipt, created = colab.acquire(source_revision(project))
        cleanup_owned = True
        worker = None
        if created:
            echo("Colabで実行環境とモデルを準備しています。")
            worker = colab.prepare(receipt)
            processes.append(worker)

        def reconnect() -> None:
            nonlocal tunnel
            if tunnel is None or tunnel.poll() is not None:
                tunnel = colab.tunnel(receipt)
                processes.append(tunnel)

        reconnect()
        session = Session(
            endpoint=f"http://127.0.0.1:{project.settings().local_port}",
            runtime_id=receipt.runtime_id,
        )
        client = ComfyClient(session.endpoint, timeout=project.settings().request_timeout)
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
                    or (worker is not None and worker.poll() is not None)
                    or (tunnel is not None and tunnel.poll() is not None)
                ):
                    raise KomaquillError(
                        "ComfyUIを起動できませんでした。準備状況を確認してください。"
                    ) from None
                time.sleep(project.settings().poll_interval)
        atomic_json(project.session_file, session.model_dump(mode="json"))
        echo("接続しました。この端末を開いたまま generate または open を実行してください。")
        force = watch(project, client, session, echo, reconnect)
    finally:
        try:
            final_receipt = colab.receipt() if cleanup_owned else None
            if final_receipt is not None:
                finish_colab(project, colab, final_receipt, client, session, force, echo, processes)
        finally:
            if client is not None:
                client.close()


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
        current_receipt = Colab(project).receipt()
        if current_receipt is not None and current_receipt.runtime_id != session.runtime_id:
            raise KomaquillError("起動処理中です。start の端末でCtrl+Cを押してください。") from None
        expected_runtime = session.runtime_id
        request_stop(project, session, force)
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
        if receipt is not None:
            processes: list[subprocess.Popen[bytes]] = []
            session = Session(
                endpoint=f"http://127.0.0.1:{project.settings().local_port}",
                runtime_id=receipt.runtime_id,
            )
            if project.session_file.is_file():
                saved_session = load_session(project)
                if (
                    saved_session.backend == "colab"
                    and saved_session.runtime_id == receipt.runtime_id
                ):
                    session = saved_session
            try:
                with colab.lock:
                    active = colab.refresh(receipt)
            except KomaquillError:
                active = True
                echo("Colabの状態を確認できませんでした。終了を再試行します。")
            client = ComfyClient(session.endpoint, timeout=project.settings().request_timeout)
            try:
                if active:
                    try:
                        verify_runtime(client, session)
                    except KomaquillError:
                        try:
                            check_port(project.settings().local_port)
                            processes.append(colab.tunnel(receipt))
                        except KomaquillError:
                            echo("接続を復旧できませんでした。取得可能なログを回収して終了します。")
                finish_colab(
                    project,
                    colab,
                    receipt,
                    client if active else None,
                    session if active else None,
                    force,
                    echo,
                    processes,
                )
            finally:
                client.close()
        elif not had_monitor and project.session_file.is_file():
            session = load_session(project)
            if session.backend == "external":
                with connected(project) as (client, session):
                    drain(project, client, session, force, echo)
        echo("終了しました。")
    finally:
        lock.release()
