"""Operate ComfyUI from the terminal."""

import secrets
import subprocess
import tomllib
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID, uuid4

import keyring
import typer
from filelock import Timeout
from pydantic import ValidationError

from komaquill import runtime
from komaquill.colab import Colab
from komaquill.downloads import fetch_model
from komaquill.errors import KomaquillError
from komaquill.models import Session
from komaquill.runtime import connected
from komaquill.service import Collection, collect, generate, verify_runtime
from komaquill.settings import Project, find_project, initialize
from komaquill.storage import atomic_json, load_runs, load_session, run_dir
from komaquill.workflow import preset, resource_json

APP_HELP = "ComfyUIで画像を生成し、ローカルへ保存します。"

app = typer.Typer(
    help=APP_HELP,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    pretty_exceptions_enable=False,
    add_completion=False,
)
models_app = typer.Typer(help="モデルを取得します。", no_args_is_help=True, add_completion=False)
app.add_typer(models_app, name="models")


def show_version(value: bool) -> None:
    """Display the installed distribution version."""
    if value:
        typer.echo(version("komaquill"))
        raise typer.Exit()


@app.command("init", help="画像と生成履歴を保存する作業フォルダーを準備します。")
def init_command(
    directory: Annotated[
        Path, typer.Argument(help="作業フォルダー。省略時は現在のフォルダー。")
    ] = Path("."),
) -> None:
    """Initialize a workspace independently of a source checkout."""
    try:
        project = initialize(directory)
    except (OSError, ValueError):
        typer.echo("作業フォルダーを準備できません。設定とアクセス権を確認してください。", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"作業フォルダーを準備しました: {project.root}")


@app.callback(help=APP_HELP)
def main(
    ctx: typer.Context,
    project: Annotated[Path | None, typer.Option("--project", help="作業フォルダー。")] = None,
    version_flag: Annotated[
        bool, typer.Option("--version", callback=show_version, is_eager=True, help="版を表示する。")
    ] = False,
) -> None:
    """Select the workspace."""
    del version_flag
    ctx.obj = project


@contextmanager
def operation(ctx: typer.Context) -> Iterator[Project]:
    """Report safe operational errors without credential-bearing tracebacks."""
    try:
        yield find_project(ctx.obj)
    except (
        KomaquillError,
        ValidationError,
        tomllib.TOMLDecodeError,
        OSError,
        keyring.errors.KeyringError,
        subprocess.SubprocessError,
    ) as exc:
        message = (
            str(exc)
            if isinstance(exc, KomaquillError)
            else "設定・保存先・資格情報を確認してください。"
        )
        typer.echo(message, err=True)
        raise typer.Exit(1) from None


def report(result: Collection) -> None:
    """Display collection outcomes."""
    typer.echo(
        f"保存 {result.saved}件 / 未回収 {result.pending}件 / 生成失敗の累計 {result.failed}件"
    )
    if result.errors:
        raise KomaquillError(
            "再回収が必要です: "
            + ", ".join(map(str, result.errors))
            + "。接続と保存先を確認し、komaquill collect を再実行してください。"
        )


@models_app.command("fetch", help="使用モデルを取得し、内容を検証します。")
def models_fetch(ctx: typer.Context) -> None:
    """Fetch and verify the selected model."""
    with operation(ctx) as project:
        typer.echo("モデルを取得・検証しています。")
        typer.echo(str(fetch_model(project.artifacts, preset())))


@app.command(help="ComfyUIへ接続し、自動回収を維持します。終了はCtrl+Cです。")
def start(
    ctx: typer.Context,
    backend: Annotated[
        Literal["colab", "external"],
        typer.Option(help="colab: Colabを起動、external: 起動済みComfyUIに接続。"),
    ] = "colab",
    endpoint: Annotated[str | None, typer.Option(help="起動済みComfyUIのURL。")] = None,
    username: Annotated[
        str | None, typer.Option(help="外部接続でBasic認証を使う場合の利用者名。")
    ] = None,
) -> None:
    """Maintain the connection and automatic collection until interrupted."""
    with operation(ctx) as project:
        password = None
        if backend == "external" and endpoint is not None:
            Session(endpoint=endpoint, backend="external", runtime_id=uuid4(), username=username)
            if username is not None:
                password = typer.prompt("ComfyUIのパスワード", hide_input=True)
        runtime.start(project, backend, endpoint, username, password, typer.echo)


@app.command("generate", help="画像を生成して保存します。")
def generate_command(
    ctx: typer.Context,
    prompt: Annotated[str | None, typer.Option(help="生成したい画像の説明。")] = None,
    negative: Annotated[str, typer.Option(help="避けたい内容。")] = "text, watermark",
    seed: Annotated[int | None, typer.Option(min=0, max=2**64 - 1, help="乱数の初期値。")] = None,
) -> None:
    """Generate an image and save its conditions."""
    with operation(ctx) as project, connected(project) as (client, session):
        text = prompt if prompt is not None else typer.prompt("生成したい画像の説明")
        if not text.strip():
            raise KomaquillError("画像の説明を入力してください。")
        typer.echo("画像を生成しています。")
        run = generate(
            project,
            client,
            session,
            text,
            negative,
            seed if seed is not None else secrets.randbits(63),
        )
        typer.echo(f"保存しました: {run_dir(project, run.run_id)}")


@app.command("collect", help="完了した画像と履歴を回収します。")
def collect_command(
    ctx: typer.Context, run_id: Annotated[UUID | None, typer.Argument()] = None
) -> None:
    """Collect completed images and generation conditions."""
    with operation(ctx) as project, connected(project) as (client, session):
        report(collect(project, client, session, run_id))


@app.command("open", help="ComfyUIの画面を開きます。")
def open_command(
    ctx: typer.Context,
    view: Annotated[
        Literal["app", "graph"], typer.Option(help="app: App Mode、graph: Node Graph。")
    ] = "app",
) -> None:
    """Open the selected ComfyUI view."""
    with operation(ctx) as project, connected(project) as (client, session):
        client.json("/system_stats")
        verify_runtime(client, session)
        if session.backend == "colab":
            url = (
                session.endpoint
                + "/?"
                + urlencode({"template": f"sd15_{view}", "source": "komaquill"})
            )
        else:
            workflow = resource_json("workflow.json")
            workflow["extra"]["linearMode"] = view == "app"
            path = project.runtime / "workflows" / f"sd15_{view}.json"
            atomic_json(path, workflow)
            typer.echo(f"画面にワークフローを読み込んでください: {path}")
            url = session.endpoint + "/"
        if not webbrowser.open(url):
            raise KomaquillError(
                f"ブラウザーを起動できません。このURLを開いてください: {url}",
                code="browser_failed",
                retryable=False,
            )
        typer.echo("ブラウザーを起動しました。自動保存には start の実行を続けてください。")


@app.command(help="接続と生成に必要なGPU・モデル・ノードを確認します。")
def doctor(ctx: typer.Context) -> None:
    """Verify the connection, GPU, model and required nodes."""
    with operation(ctx) as project, connected(project) as (client, session):
        verify_runtime(client, session)
        stats = client.json("/system_stats")
        nodes = client.json("/object_info")
        missing = {
            n["class_type"] for n in resource_json("workflow-api.json").values()
        } - nodes.keys()
        if missing:
            raise KomaquillError(
                "生成に必要な標準ノードがありません。ComfyUIの構成を確認してください。"
            )
        checkpoint = nodes["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        if preset().filename not in checkpoint:
            raise KomaquillError(
                "使用モデルがComfyUIにありません。モデルの配置を確認してください。"
            )
        if not any(device.get("type") == "cuda" for device in stats.get("devices", [])):
            raise KomaquillError("GPUが見つかりません。接続先のGPUを確認してください。")
        typer.echo(f"接続・GPU・モデル・標準ノードを確認しました。保存先: {project.artifacts}")


@app.command(help="接続・Colab・未回収の状態を確認します。")
def status(ctx: typer.Context) -> None:
    """Display runtime, monitor and collection state."""
    with operation(ctx) as project:
        project = runtime.session_project(project)
        colab = Colab(project)
        receipt = colab.receipt()
        active: bool | None = None
        if receipt is not None:
            try:
                with colab.lock.acquire(timeout=0):
                    active = colab.refresh(receipt)
                if not active and receipt.endpoint_id is None:
                    typer.echo(
                        "Colab: 確保結果が未確認です。Colabの利用中ランタイムを確認してください。"
                    )
                else:
                    typer.echo(
                        "Colab: 利用中"
                        if active
                        else "Colab: 終了済み。stop で管理記録を整理できます。"
                    )
            except Timeout:
                typer.echo("Colab: 起動または終了の処理中")
        else:
            typer.echo("Colab: 所有するランタイムはありません。")
        lock = runtime.monitor_lock(project)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            typer.echo("自動回収: 実行中")
        else:
            lock.release()
            typer.echo("自動回収: 停止中")
        if (
            project.session_file.is_file()
            and receipt is None
            and load_session(project).backend == "colab"
        ):
            typer.echo("ComfyUI: 終了済み")
        elif receipt is not None and active is False:
            typer.echo("ComfyUI: 未起動" if receipt.endpoint_id is None else "ComfyUI: 終了済み")
        elif receipt is not None and (
            not project.session_file.is_file()
            or load_session(project).runtime_id != receipt.runtime_id
        ):
            typer.echo("ComfyUI: 準備中")
        elif project.session_file.is_file():
            try:
                with connected(project) as (client, session):
                    client.json("/system_stats")
                    verify_runtime(client, session)
                typer.echo("ComfyUI: 接続できます。")
            except KomaquillError:
                typer.echo("ComfyUI: 接続できません。start の実行状態を確認してください。")
        pending = [str(run.run_id) for run in load_runs(project) if run.pending]
        typer.echo(f"未回収: {len(pending)}件")
        lost = [str(run.run_id) for run in load_runs(project) if run.state == "lost"]
        typer.echo(f"回収不能: {len(lost)}件")
        if pending:
            typer.echo("\n".join(pending))


@app.command(help="生成と回収を待ち、所有するColabを終了します。")
def stop(
    ctx: typer.Context,
    force: Annotated[
        bool, typer.Option(help="生成完了を待たず、回収可能な結果を保存して終了する。")
    ] = False,
) -> None:
    """Stop the owned runtime after collecting available results."""
    with operation(ctx) as project:
        runtime.stop(project, force, typer.echo)
