"""接続の維持、自動回収と終了を行う。"""

import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4

import keyring
from filelock import FileLock, Timeout
from pydantic import ValidationError

from komaquill.client import ComfyClient
from komaquill.colab import Colab, Receipt, check_port, terminate
from komaquill.deployment import create_payload
from komaquill.downloads import fetch_model
from komaquill.errors import KomaquillError, RuntimeEnded
from komaquill.events import failure_info, record_event
from komaquill.models import Session, ShutdownResult
from komaquill.service import (
    Collection,
    collect,
    collection_lock,
    mark_lost,
    verify_runtime,
)
from komaquill.settings import Project
from komaquill.storage import atomic_json, load_runs, load_session, stop_request
from komaquill.workflow import preset


def session_project(project: Project) -> Project:
    """Use saved settings for status and stop even if the source file is broken."""
    try:
        session = load_session(project)
    except (OSError, ValueError):
        return project
    return project.configured(session.settings) if session.settings is not None else project


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
        session.endpoint,
        password,
        (session.settings or project.settings()).request_timeout,
        username=session.username,
    )
    try:
        yield client, session
    finally:
        client.close()


def monitor_lock(project: Project) -> FileLock:
    """二重起動と終了中の再起動を防ぐ。"""
    project.state.mkdir(parents=True, exist_ok=True)
    return FileLock(str(project.state / ".monitor.lock"))


def excluded_history(client: ComfyClient) -> set[str]:
    """接続前のキューと履歴を回収対象から外す。"""
    excluded = {str(row[1]) for row in client.queue()}
    excluded.update(client.history())
    return excluded


def request_stop(project: Project, session: Session, force: bool) -> None:
    """並行する終了要求で強制終了を通常終了へ戻さない。"""
    with collection_lock(project), FileLock(str(project.state / ".stop.lock")):
        force = force or stop_request(project, session) is True
        atomic_json(
            project.state / "stop.json", {"runtime_id": str(session.runtime_id), "force": force}
        )


def drain(
    project: Project,
    client: ComfyClient,
    session: Session,
    force: bool,
    echo: Callable[[str], None],
) -> Collection:
    """Wait within the startup snapshot and return collection outcomes explicitly."""
    project = project.configured(session.settings)
    settings = project.settings()
    deadline = time.monotonic() + settings.shutdown_timeout
    result = Collection(0, 0, 0)
    saved = 0
    previous_errors: tuple[UUID | str, ...] = ()
    while True:
        try:
            try:
                result = collect(project, client, session)
                saved += result.saved
                if result.saved:
                    echo(f"保存しました: {result.saved}件")
                if result.errors and result.errors != previous_errors:
                    echo("再回収が必要です: " + ", ".join(map(str, result.errors)))
                previous_errors = result.errors
            except KomaquillError as error:
                unreadable: list[UUID | str] = []
                pending = tuple(
                    run.run_id
                    for run in load_runs(project, unreadable=unreadable)
                    if run.runtime_id == session.runtime_id and run.pending
                )
                echo("接続できないため回収を完了できませんでした。管理記録を保持します。")
                return Collection(
                    saved,
                    len(pending),
                    0,
                    tuple(unreadable),
                    pending,
                    failure_info(error, "collection"),
                )
            if (
                force
                or stop_request(project, session) is True
                or not result.pending_ids
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(settings.poll_interval)
        except KeyboardInterrupt:
            force = True
            request_stop(project, session, True)
    if result.pending_ids:
        echo("未完了・未回収: " + ", ".join(map(str, result.pending_ids)))
    return Collection(saved, result.pending, result.failed, result.errors, result.pending_ids)


def watch(
    project: Project,
    client: ComfyClient,
    session: Session,
    echo: Callable[[str], None],
    reconnect: Callable[[], None] | None = None,
) -> bool:
    """接続と回収を維持し、終了時に待機方法を返す。"""
    project = project.configured(session.settings)
    disconnected = False
    previous_errors: tuple[UUID | str, ...] = ()
    try:
        while stop_request(project, session) is None:
            try:
                if reconnect is not None:
                    reconnect()
                result = collect(project, client, session)
                if disconnected:
                    echo("再接続しました。")
                    record_event(project, "connection", "restored", runtime_id=session.runtime_id)
                if result.saved:
                    echo(f"保存しました: {result.saved}件")
                if result.errors and result.errors != previous_errors:
                    echo("再回収が必要です: " + ", ".join(map(str, result.errors)))
                previous_errors = result.errors
                disconnected = False
            except RuntimeEnded:
                record_event(project, "connection", "runtime_ended", runtime_id=session.runtime_id)
                return True
            except KomaquillError as error:
                if not disconnected:
                    record_event(
                        project,
                        "connection",
                        "disconnected",
                        runtime_id=session.runtime_id,
                        failure=failure_info(error, "connection"),
                    )
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
    """Attempt every shutdown phase and release ownership only after durable finalization."""
    project = project.configured(receipt.settings)
    result = ShutdownResult(runtime_id=receipt.runtime_id, force=force)
    fatal: list[str] = []
    messages: list[str] = []
    released = False

    def failed(stage: str, message: str, error: BaseException, *, retain: bool = True) -> None:
        result.errors.append(failure_info(error, stage))
        messages.append(message)
        if retain:
            fatal.append(stage)
        record_event(
            project, stage, "failed", runtime_id=receipt.runtime_id, failure=result.errors[-1]
        )

    def announce(message: str) -> None:
        try:
            echo(message)
        except (OSError, KeyboardInterrupt) as error:
            failed("display", "終了結果を端末に表示できません。", error)

    try:
        if client is not None and session is not None:
            try:
                request_stop(project, session, force)
                report = drain(project, client, session, force, echo)
                result.force = force or stop_request(project, session) is True
                result.collection = "ok"
                if report is not None:
                    result.pending_ids = list(report.pending_ids)
                    if report.errors or report.failure:
                        result.collection = "failed"
                        if report.failure:
                            result.errors.append(report.failure)
                        else:
                            result.errors.append(
                                failure_info(
                                    KomaquillError("", code="collection_failed"), "collection"
                                )
                            )
                        messages.append("画像を一部回収できませんでした。")
            except (Exception, KeyboardInterrupt) as error:
                result.collection = "failed"
                failed("collection", "画像の回収を完了できませんでした。", error)
        try:
            failed_logs = colab.recover_diagnostics(receipt)
            result.logs = "failed" if failed_logs else "ok"
            if failed_logs:
                failed(
                    "log_collection",
                    "ログを一部回収できませんでした。",
                    KomaquillError("", code="log_unavailable"),
                    retain=False,
                )
        except (KomaquillError, OSError, KeyboardInterrupt) as error:
            result.logs = "failed"
            failed("log_collection", "ログを回収できませんでした。", error, retain=False)
        except Exception as error:
            result.logs = "failed"
            failed("log_collection", "ログの回収で障害が発生しました。", error)
    finally:
        try:
            try:
                colab.stop()
            except KeyboardInterrupt:
                announce("Colabの終了を再確認しています。")
                colab.stop()
            result.gpu = "stopped"
        except (Exception, KeyboardInterrupt) as error:
            result.gpu = "unknown"
            failed("gpu_stop", "Colabの終了を確認できません。", error)
        finally:
            for process in processes:
                try:
                    terminate(process, project.settings().control_timeout)
                except (Exception, KeyboardInterrupt) as error:
                    result.connections = "failed"
                    failed("disconnect", "ローカルの接続処理を終了できません。", error)
            try:
                colab.cleanup_processes(receipt)
            except (Exception, KeyboardInterrupt) as error:
                result.connections = "failed"
                failed("cleanup", "ローカルの実行待ち処理を終了できません。", error)
    if result.gpu == "stopped":
        try:
            result.lost_ids = list(mark_lost(project, receipt.runtime_id))
            result.history = "ok"
            if result.lost_ids:
                announce("回収不能: " + ", ".join(map(str, result.lost_ids)))
                if not result.force:
                    result.collection = "incomplete"
                    failed(
                        "collection",
                        "待機上限までに保存できなかった画像があります。",
                        KomaquillError("", code="shutdown_timeout", retryable=False),
                        retain=False,
                    )
        except (Exception, KeyboardInterrupt) as error:
            result.history = "failed"
            failed("finalize", "生成履歴の終了状態を保存できません。", error)
    result.finished_at = datetime.now(UTC)
    folder = project.artifacts / "logs" / str(receipt.runtime_id)
    try:
        atomic_json(folder / "shutdown.json", result.model_dump(mode="json"))
    except OSError as error:
        failed("summary", "終了結果を保存できません。", error)
    announce(f"画像回収: {result.collection} / ログ取得: {result.logs} / Colab: {result.gpu}")
    if result.gpu == "stopped" and not fatal:
        try:
            colab.release(receipt)
            released = True
        except (Exception, KeyboardInterrupt) as error:
            failed("release", "所有記録を整理できません。", error)
    if fatal:
        with suppress(OSError):
            atomic_json(folder / "shutdown.json", result.model_dump(mode="json"))
    if result.errors:
        suffix = (
            " 終了結果をログで確認してください。"
            if released
            else " 管理記録を残しました。stop で再確認してください。"
        )
        raise KomaquillError(" ".join(messages) + suffix)


def finish_external(
    project: Project,
    client: ComfyClient,
    session: Session,
    force: bool,
    echo: Callable[[str], None],
) -> None:
    """Finalize local monitoring without stopping or invalidating the external server."""
    project = project.configured(session.settings)
    result = ShutdownResult(
        runtime_id=session.runtime_id, force=force, gpu="not_managed", logs="not_managed"
    )
    try:
        request_stop(project, session, force)
        report = drain(project, client, session, force, echo)
        result.collection = "ok"
        result.history = "ok"
        if report is not None:
            result.pending_ids = list(report.pending_ids)
            if report.failure or report.errors:
                result.collection = "failed"
                result.errors.append(
                    report.failure
                    or failure_info(KomaquillError("", code="collection_failed"), "collection")
                )
    except (Exception, KeyboardInterrupt) as error:
        result.collection = "failed"
        result.errors.append(failure_info(error, "collection"))
    result.finished_at = datetime.now(UTC)
    atomic_json(
        project.artifacts / "logs" / str(session.runtime_id) / "shutdown.json",
        result.model_dump(mode="json"),
    )
    record_event(project, "monitor", "stopped", runtime_id=session.runtime_id)
    if result.errors:
        raise KomaquillError(
            "画像の回収を完了できませんでした。接続と保存先を確認してcollectを実行してください。"
        )


def start_external(
    project: Project,
    endpoint: str,
    username: str | None,
    password: str | None,
    echo: Callable[[str], None],
) -> None:
    """起動済みComfyUIへの接続だけを維持する。"""
    project = project.configured()
    session = Session(
        endpoint=endpoint,
        backend="external",
        runtime_id=uuid4(),
        username=username,
        settings=project.settings(),
    )
    if project.session_file.is_file():
        prior = load_session(project)
        if (
            prior.backend == "external"
            and prior.endpoint == session.endpoint
            and prior.username == username
        ):
            session = prior.model_copy(
                update={"username": username, "settings": project.settings()}
            )
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
        record_event(project, "connection", "ready", runtime_id=session.runtime_id)
        echo("接続しました。この端末を開いたまま generate または open を実行してください。")
        ready = True
        force = watch(project, client, session, echo)
    finally:
        try:
            if ready:
                finish_external(project, client, session, force, echo)
        finally:
            client.close()


def start_colab(project: Project, echo: Callable[[str], None]) -> None:
    """確保から終了確認まで一つのColabを所有する。"""
    requested = project.settings()
    colab = Colab(project)
    saved = colab.receipt()
    if saved is not None and saved.settings is not None and requested != saved.settings:
        raise KomaquillError(
            "前回の起動設定と異なります。stop 後にstartして設定を反映してください。"
        )
    project = project.configured(colab.settings)
    processes: list[subprocess.Popen[bytes]] = []
    tunnel: subprocess.Popen[bytes] | None = None
    client: ComfyClient | None = None
    session: Session | None = None
    cleanup_owned = False
    force = True
    try:
        check_port(project.settings().local_port)
        if not record_event(project, "start", "preparing"):
            raise KomaquillError("操作ログの保存先を確認してください。", code="diagnostics_failed")
        echo("モデルを取得・検証しています。")
        fetch_model(project.artifacts, preset())
        payload = create_payload(project)
        cleanup_owned = colab.receipt() is None
        receipt, created = colab.acquire(payload.code_sha256)
        cleanup_owned = True
        worker = None
        if created:
            echo("Colabで実行環境とモデルを準備しています。")
            worker = colab.prepare(receipt, payload)
            processes.append(worker)

        def reconnect() -> None:
            nonlocal tunnel
            if tunnel is None or tunnel.poll() is not None:
                with colab.lock:
                    if not colab.refresh(receipt):
                        raise RuntimeEnded(
                            "Colabは終了しています。", code="runtime_ended", retryable=False
                        )
                tunnel = colab.tunnel(receipt)
                processes.append(tunnel)

        reconnect()
        session = Session(
            endpoint=f"http://127.0.0.1:{project.settings().local_port}",
            runtime_id=receipt.runtime_id,
            settings=project.settings(),
        )
        client = ComfyClient(session.endpoint, timeout=project.settings().request_timeout)
        deadline = time.monotonic() + project.settings().preparation_timeout
        while True:
            try:
                metadata = client.runtime()
                session = Session.model_validate(
                    {**metadata, "endpoint": session.endpoint, "settings": project.settings()}
                )
                if session.runtime_id != receipt.runtime_id:
                    raise KomaquillError("ColabのランタイムIDが所有記録と異なります。")
                if receipt.code_sha256 is not None and session.code_sha256 != receipt.code_sha256:
                    raise KomaquillError(
                        "GPU側の実行コードが一致しません。stop後にstartしてください。"
                    )
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
        record_event(project, "connection", "ready", runtime_id=session.runtime_id)
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
            (project.state / "stop.json").unlink(missing_ok=True)
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
    project = session_project(project)
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
        project = project.configured(session.settings)
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
            project = project.configured(receipt.settings)
            processes: list[subprocess.Popen[bytes]] = []
            session = Session(
                endpoint=f"http://127.0.0.1:{project.settings().local_port}",
                runtime_id=receipt.runtime_id,
                settings=project.settings(),
            )
            client: ComfyClient | None = None
            active = True
            try:
                if project.session_file.is_file():
                    try:
                        saved_session = load_session(project)
                        if (
                            saved_session.backend == "colab"
                            and saved_session.runtime_id == receipt.runtime_id
                        ):
                            session = saved_session
                    except (OSError, ValidationError):
                        echo("接続情報を読めないため、所有記録を使って終了します。")
                try:
                    with colab.lock:
                        active = colab.refresh(receipt)
                except KomaquillError:
                    echo("Colabの状態を確認できませんでした。終了を再試行します。")
                client = ComfyClient(session.endpoint, timeout=project.settings().request_timeout)
                if active:
                    try:
                        verify_runtime(client, session)
                    except KomaquillError:
                        try:
                            check_port(project.settings().local_port)
                            processes.append(colab.tunnel(receipt))
                        except (KomaquillError, OSError, subprocess.SubprocessError):
                            echo("接続を復旧できませんでした。取得可能なログを回収して終了します。")
            finally:
                try:
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
                    if client is not None:
                        client.close()
        elif not had_monitor and project.session_file.is_file():
            session = load_session(project)
            if session.backend == "external":
                with connected(project) as (client, session):
                    finish_external(project, client, session, force, echo)
        if had_monitor and expected_runtime is not None:
            path = project.artifacts / "logs" / str(expected_runtime) / "shutdown.json"
            if path.is_file() and ShutdownResult.model_validate_json(path.read_bytes()).errors:
                raise KomaquillError(
                    "終了処理の一部が失敗しました。ログの終了結果を確認してください。"
                )
        echo("終了しました。")
    finally:
        lock.release()
