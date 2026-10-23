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

from komaquill.application.generation import Collection, collect, generate, verify_runtime
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import Session
from komaquill.infrastructure import runtime as runtime
from komaquill.infrastructure.colab import Colab
from komaquill.infrastructure.configuration import Project, find_project, initialize
from komaquill.infrastructure.downloads import fetch_model
from komaquill.infrastructure.runtime import connected
from komaquill.infrastructure.storage import atomic_json, load_runs, load_session, run_dir
from komaquill.infrastructure.workflows import preset, resource_json
from komaquill.messages import error_message, message

APP_HELP = message("text.generate_images_with_comfyui_and_save_them_locally")

app = typer.Typer(
    help=APP_HELP,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    pretty_exceptions_enable=False,
    add_completion=False,
)
models_app = typer.Typer(
    help=message("text.manage_the_image_model"), no_args_is_help=True, add_completion=False
)
app.add_typer(models_app, name="models")


def show_version(value: bool) -> None:
    """Display the installed distribution version."""
    if value:
        typer.echo(version("komaquill"))
        raise typer.Exit()


@app.command("init", help=message("text.initialize_a_workspace_for_images_and_generation_history"))
def init_command(
    directory: Annotated[
        Path,
        typer.Argument(help=message("text.workspace_directory_defaults_to_the_current_directory")),
    ] = Path("."),
) -> None:
    """Initialize a workspace independently of a source checkout."""
    try:
        project = initialize(directory)
    except (OSError, ValueError):
        typer.echo(
            message("text.cannot_initialize_the_workspace_check_its_configuration_and"), err=True
        )
        raise typer.Exit(1) from None
    typer.echo(message("text.workspace_initialized_root", root=project.root))


@app.callback(help=APP_HELP)
def main(
    ctx: typer.Context,
    project: Annotated[
        Path | None, typer.Option("--project", help=message("text.workspace_directory"))
    ] = None,
    version_flag: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=show_version,
            is_eager=True,
            help=message("text.show_the_installed_version"),
        ),
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
        user_message = (
            error_message(exc)
            if isinstance(exc, KomaquillError)
            else message("text.check_the_configuration_storage_permissions_and_credentials")
        )
        typer.echo(user_message, err=True)
        raise typer.Exit(1) from None
    except typer.Exit:
        raise
    except Exception as exc:
        from komaquill.domain.errors import failure_info
        from komaquill.infrastructure.diagnostics import record_event

        record_event(find_project(ctx.obj), "cli", "failed", failure=failure_info(exc, "operation"))
        typer.echo(message("error.internal_error"), err=True)
        raise typer.Exit(1) from None


def report(result: Collection) -> None:
    """Display per-invocation collection outcomes independently of historical failures."""
    typer.echo(
        message(
            "collection.summary", saved=result.saved, pending=result.pending, failed=result.failed
        )
    )
    if result.errors:
        typer.echo(message("collection.retry", ids=", ".join(map(str, result.errors))), err=True)
        raise typer.Exit(1)


@models_app.command("fetch", help=message("text.download_and_verify_the_selected_model"))
def models_fetch(ctx: typer.Context) -> None:
    """Fetch and verify the selected model."""
    with operation(ctx) as project:
        typer.echo(message("text.downloading_and_verifying_the_model"))
        typer.echo(str(fetch_model(project.artifacts, preset())))


@app.command(help=message("text.start_comfyui_and_collect_results_automatically_press_ctrl"))
def start(
    ctx: typer.Context,
    backend: Annotated[
        Literal["colab", "external"],
        typer.Option(help=message("text.colab_start_colab_external_connect_to_a_running")),
    ] = "colab",
    endpoint: Annotated[
        str | None, typer.Option(help=message("text.url_of_a_running_comfyui_server"))
    ] = None,
    username: Annotated[
        str | None,
        typer.Option(
            help=message("text.username_for_optional_basic_authentication_on_an_external")
        ),
    ] = None,
) -> None:
    """Maintain the connection and automatic collection until interrupted."""
    with operation(ctx) as project:
        password = None
        if backend == "external" and endpoint is not None:
            Session(endpoint=endpoint, backend="external", runtime_id=uuid4(), username=username)
            if username is not None:
                password = typer.prompt(message("auth.prompt"), hide_input=True)
        runtime.start(project, backend, endpoint, username, password, typer.echo)


@app.command("generate", help=message("text.generate_and_save_an_image"))
def generate_command(
    ctx: typer.Context,
    prompt: Annotated[
        str | None, typer.Option(help=message("text.description_of_the_image_to_generate"))
    ] = None,
    negative: Annotated[
        str, typer.Option(help=message("text.content_to_avoid"))
    ] = "text, watermark",
    seed: Annotated[
        int | None, typer.Option(min=0, max=2**64 - 1, help=message("text.random_seed"))
    ] = None,
) -> None:
    """Generate an image and save its conditions."""
    with operation(ctx) as project, connected(project) as (client, session):
        text = (
            prompt
            if prompt is not None
            else typer.prompt(message("text.describe_the_image_to_generate"))
        )
        if not text.strip():
            raise KomaquillError(display_key="text.enter_a_nonempty_image_description")
        typer.echo(message("text.generating_the_image"))
        run = generate(
            project,
            client,
            session,
            text,
            negative,
            seed if seed is not None else secrets.randbits(63),
        )
        typer.echo(message("text.saved_directory", directory=run_dir(project, run.run_id)))


@app.command("collect", help=message("text.collect_completed_images_and_generation_history"))
def collect_command(
    ctx: typer.Context, run_id: Annotated[UUID | None, typer.Argument()] = None
) -> None:
    """Collect completed images and generation conditions."""
    with operation(ctx) as project, connected(project) as (client, session):
        report(collect(project, client, session, run_id))


@app.command("open", help=message("text.open_a_comfyui_view_in_the_browser"))
def open_command(
    ctx: typer.Context,
    view: Annotated[Literal["app", "graph"], typer.Option(help=message("cli.view_help"))] = "app",
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
            typer.echo(message("text.load_this_workflow_in_comfyui_path", path=path))
            url = session.endpoint + "/"
        if not webbrowser.open(url):
            raise KomaquillError(
                display_key="text.cannot_open_the_browser_open_this_url_url",
                details={"url": str(url)},
                code="browser_failed",
                retryable=False,
            )
        typer.echo(message("text.browser_opened_keep_start_running_to_save_results"))


@app.command(help=message("text.check_the_connection_gpu_model_and_required_nodes"))
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
                display_key="text.required_standard_nodes_are_missing_check_the_comfyui"
            )
        checkpoint = client.checkpoints()
        if preset().filename not in checkpoint:
            raise KomaquillError(display_key="text.the_selected_model_is_missing_check_the_comfyui")
        if not any(device.get("type") == "cuda" for device in stats.get("devices", [])):
            raise KomaquillError(display_key="text.no_cuda_device_was_found_check_the_server")
        typer.echo(
            message(
                "text.connection_gpu_model_and_required_nodes_verified_storage",
                artifacts=project.artifacts,
            )
        )


@app.command(help=message("text.show_the_connection_runtime_and_pending_results"))
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
                        message("text.colab_allocation_unconfirmed_check_the_active_colab_runtimes")
                    )
                else:
                    typer.echo(
                        message("text.colab_active")
                        if active
                        else message("text.colab_stopped_run_stop_to_finalize_ownership_records")
                    )
            except Timeout:
                typer.echo(message("text.colab_starting_or_stopping"))
        else:
            typer.echo(message("text.colab_no_owned_runtime"))
        lock = runtime.monitor_lock(project)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            typer.echo(message("text.collector_running"))
        else:
            lock.release()
            typer.echo(message("text.collector_stopped"))
        if (
            project.session_file.is_file()
            and receipt is None
            and load_session(project).backend == "colab"
        ):
            typer.echo(message("text.comfyui_stopped"))
        elif receipt is not None and active is False:
            typer.echo(
                message("text.comfyui_not_started")
                if receipt.endpoint_id is None
                else message("text.comfyui_stopped")
            )
        elif receipt is not None and (
            not project.session_file.is_file()
            or load_session(project).runtime_id != receipt.runtime_id
        ):
            typer.echo(message("text.comfyui_preparing"))
        elif project.session_file.is_file():
            try:
                with connected(project) as (client, session):
                    client.json("/system_stats")
                    verify_runtime(client, session)
                typer.echo(message("text.comfyui_connected"))
            except KomaquillError:
                typer.echo(message("text.comfyui_disconnected_check_the_start_process"))
        pending = [str(run.run_id) for run in load_runs(project) if run.pending]
        typer.echo(message("text.pending_pending_count", pending_count=len(pending)))
        lost = [str(run.run_id) for run in load_runs(project) if run.state == "lost"]
        typer.echo(message("text.lost_lost_count", lost_count=len(lost)))
        if pending:
            typer.echo("\n".join(pending))


@app.command(help=message("text.wait_for_results_and_stop_the_owned_runtime"))
def stop(
    ctx: typer.Context,
    force: Annotated[
        bool, typer.Option(help=message("text.save_available_results_and_stop_without_waiting_for"))
    ] = False,
) -> None:
    """Stop the owned runtime after collecting available results."""
    with operation(ctx) as project:
        runtime.stop(project, force, typer.echo)
