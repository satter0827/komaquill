"""人が端末またはVS Codeから使う操作の入口。"""

import secrets
import time
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID, uuid4

import keyring
import typer
from pydantic import ValidationError

from komaquill.client import ComfyClient
from komaquill.downloads import fetch_model
from komaquill.errors import KomaquillError
from komaquill.models import Session
from komaquill.service import Collection, collect, generate, source_revision
from komaquill.settings import Project, find_project
from komaquill.storage import atomic_json, load_session, run_dir
from komaquill.workflow import preset, resource_json

app = typer.Typer(
    help="ColabのComfyUIで画像を生成し、ローカルへ保存します。",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
models_app = typer.Typer(help="モデルを取得します。", no_args_is_help=True)
app.add_typer(models_app, name="models")


def show_version(value: bool) -> None:
    """パッケージと同じバージョンを表示する。"""
    if value:
        typer.echo(version("komaquill"))
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    project: Annotated[Path | None, typer.Option("--project", help="作業フォルダー。")] = None,
    version_flag: Annotated[
        bool, typer.Option("--version", callback=show_version, is_eager=True, help="版を表示する。")
    ] = False,
) -> None:
    """プロジェクトを選ぶ。"""
    del version_flag
    ctx.obj = project


@contextmanager
def operation(ctx: typer.Context) -> Iterator[Project]:
    """操作エラーを日本語で表示し、秘密情報を含むトレースを出さない。"""
    try:
        yield find_project(ctx.obj)
    except (KomaquillError, ValidationError, OSError, keyring.errors.KeyringError) as exc:
        message = (
            str(exc)
            if isinstance(exc, KomaquillError)
            else "設定・保存先・資格情報を確認してください。"
        )
        typer.echo(message, err=True)
        raise typer.Exit(1) from None


def credential_key(project: Project) -> str:
    """別プロジェクトのパスワードを混用しない。"""
    return str(project.root)


@contextmanager
def connected(project: Project) -> Iterator[tuple[ComfyClient, Session]]:
    """保存済み接続先を開く。"""
    if not project.session_file.is_file():
        raise KomaquillError("接続先が未設定です。start を実行してください。")
    session = load_session(project)
    password = keyring.get_password("komaquill", credential_key(project))
    if not password:
        password = typer.prompt("ComfyUIのパスワード", hide_input=True)
    client = ComfyClient(session.endpoint, password, project.settings().request_timeout)
    try:
        yield client, session
    finally:
        client.close()


def report(result: Collection) -> None:
    """回収状況を表示する。"""
    typer.echo(f"保存 {result.saved}件 / 未回収 {result.pending}件 / 失敗 {result.failed}件")


@models_app.command("fetch")
def models_fetch(ctx: typer.Context) -> None:
    """使用モデルを取得し、内容を検証します。"""
    with operation(ctx) as project:
        typer.echo("モデルを取得・検証しています。")
        typer.echo(str(fetch_model(project.artifacts, preset())))


@app.command()
def start(
    ctx: typer.Context,
    endpoint: Annotated[str | None, typer.Option(help="Colabに表示された接続先URL。")] = None,
) -> None:
    """Colabを開き、起動後のComfyUIへ接続します。"""
    with operation(ctx) as project:
        if endpoint is None:
            typer.echo("モデルを取得・検証しています。")
            fetch_model(project.artifacts, preset())
            revision = source_revision(project)
            notebook = (
                "https://colab.research.google.com/github/satter0827/komaquill/"
                f"blob/{revision}/notebooks/comfyui.ipynb"
            )
            typer.echo("ColabでGPUを選び、ノートブックを実行してください。")
            typer.echo(notebook)
            typer.echo(f"コミットID: {revision}")
            webbrowser.open(notebook)
            endpoint = typer.prompt("Colabに表示された接続先URL")
        # URLを先に検証し、認証情報を意図しない接続先へ送らない。
        probe = Session(
            endpoint=endpoint,
            runtime_id=uuid4(),
            source_revision="",
            comfy_revision="",
            frontend_version="",
            model_sha256="",
        )
        password = typer.prompt("ComfyUIのパスワード", hide_input=True)
        client = ComfyClient(probe.endpoint, password, project.settings().request_timeout)
        try:
            metadata = client.runtime()
            session = Session.model_validate({**metadata, "endpoint": probe.endpoint})
            client.json("/system_stats")
        finally:
            client.close()
        keyring.set_password("komaquill", credential_key(project), password)
        atomic_json(project.session_file, session.model_dump(mode="json"))
        typer.echo("接続しました。generate または open を実行できます。")


@app.command("generate")
def generate_command(
    ctx: typer.Context,
    prompt: Annotated[str | None, typer.Option(help="生成したい画像の説明。")] = None,
    negative: Annotated[str, typer.Option(help="避けたい内容。")] = "text, watermark",
    seed: Annotated[int | None, typer.Option(min=0, max=2**64 - 1, help="乱数の初期値。")] = None,
) -> None:
    """画像を生成して保存します。"""
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


@app.command("collect")
def collect_command(
    ctx: typer.Context, run_id: Annotated[UUID | None, typer.Argument()] = None
) -> None:
    """完了した画像と履歴を回収します。"""
    with operation(ctx) as project, connected(project) as (client, session):
        report(collect(project, client, session, run_id))


@app.command("open")
def open_command(
    ctx: typer.Context,
    view: Annotated[str, typer.Option(help="app: 入力画面、graph: ノード画面。")] = "app",
) -> None:
    """ComfyUIの画面を開き、完了した画像を自動保存します。終了はCtrl+Cです。"""
    if view not in {"app", "graph"}:
        raise typer.BadParameter("app または graph を指定してください。", param_hint="--view")
    with operation(ctx) as project, connected(project) as (client, session):
        result = collect(project, client, session)
        url = (
            session.endpoint + "/?" + urlencode({"template": f"sd15_{view}", "source": "komaquill"})
        )
        webbrowser.open(url)
        typer.echo("画面で生成した画像を保存します。終了はCtrl+Cです。")
        report(result)
        disconnected = False
        try:
            while True:
                time.sleep(project.settings().poll_interval)
                try:
                    result = collect(project, client, session)
                    if disconnected or result.saved:
                        report(result)
                    disconnected = False
                except KomaquillError as exc:
                    if not disconnected:
                        typer.echo(str(exc), err=True)
                        typer.echo(
                            "再接続を待っています。接続先が変わった場合はstartを実行してください。"
                        )
                    disconnected = True
        except KeyboardInterrupt:
            try:
                report(collect(project, client, session))
            except KomaquillError:
                typer.echo(
                    "回収できていない結果があります。接続後にcollectを実行してください。", err=True
                )
            typer.echo("自動保存を終了しました。利用後はColabのランタイムを終了してください。")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """接続と生成に必要なモデル・ノードを確認します。"""
    with operation(ctx) as project, connected(project) as (client, session):
        metadata = client.runtime()
        if str(metadata.get("runtime_id")) != str(session.runtime_id):
            raise KomaquillError("ランタイムが変わりました。start を実行してください。")
        stats = client.json("/system_stats")
        nodes = client.json("/object_info")
        missing = {
            n["class_type"] for n in resource_json("workflow-api.json").values()
        } - nodes.keys()
        if missing:
            raise KomaquillError("必要な標準ノードがありません。Colabの準備をやり直してください。")
        checkpoint = nodes["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        if preset().filename not in checkpoint:
            raise KomaquillError("使用モデルがColabにありません。ノートブックを確認してください。")
        if not any(device.get("type") == "cuda" for device in stats.get("devices", [])):
            raise KomaquillError("GPUが見つかりません。ColabでGPUを選択してください。")
        typer.echo(f"接続・GPU・モデル・標準ノードを確認しました。保存先: {project.artifacts}")
