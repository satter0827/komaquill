"""Operate ComfyUI from the terminal."""

import secrets
import subprocess
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

import keyring
import typer
from filelock import Timeout
from pydantic import ValidationError

from komaquill.application.generation import Collection, collect, generate, verify_runtime
from komaquill.domain.assets import Preparation
from komaquill.domain.errors import FailureInfo, KomaquillError
from komaquill.domain.materials import Material
from komaquill.domain.models import Session
from komaquill.infrastructure import runtime as runtime
from komaquill.infrastructure import supervisor
from komaquill.infrastructure.colab import Colab
from komaquill.infrastructure.comfyui import input_choices
from komaquill.infrastructure.configuration import Project, find_project, initialize
from komaquill.infrastructure.downloads import fetch_model
from komaquill.infrastructure.materials import MaterialCollector
from komaquill.infrastructure.monitor_state import alive, read_monitor
from komaquill.infrastructure.preparation import capabilities
from komaquill.infrastructure.runtime import connected
from komaquill.infrastructure.storage import load_runs, load_session, run_dir
from komaquill.infrastructure.views import open_view
from komaquill.infrastructure.workflows import assets, prepare, select
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
def models_fetch(
    ctx: typer.Context,
    profile: Annotated[list[str] | None, typer.Option(help=message("profile.start_help"))] = None,
) -> None:
    """Fetch and verify the selected model."""
    with operation(ctx) as project:
        typer.echo(message("text.downloading_and_verifying_the_model"))
        settings = project.settings()
        if profile:
            settings = settings.model_copy(update={"profiles": tuple(profile)})
            settings = type(settings).model_validate(settings.model_dump())
        for asset in assets(settings):
            typer.echo(str(fetch_model(project, asset)))


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
    profile: Annotated[list[str] | None, typer.Option(help=message("profile.start_help"))] = None,
    foreground: Annotated[bool, typer.Option(help=message("monitor.foreground_help"))] = False,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help=message("profile.open_help"))
    ] = True,
) -> None:
    """Maintain the connection and automatic collection until interrupted."""
    with operation(ctx) as project:
        settings = project.settings()
        if profile:
            settings = type(settings).model_validate({**settings.model_dump(), "profiles": profile})
        project = project.configured(settings)
        password = None
        if backend == "external" and endpoint is not None:
            Session(endpoint=endpoint, backend="external", runtime_id=uuid4(), username=username)
            if username is not None:
                password = typer.prompt(message("auth.prompt"), hide_input=True)

        if password is not None and endpoint is not None:
            temporary = Session(
                endpoint=endpoint, backend="external", runtime_id=uuid4(), username=username
            )
            keyring.set_password("komaquill", runtime.credential_key(project, temporary), password)
        from komaquill.interfaces.session_worker import main as session_main

        supervisor.start(
            project,
            backend,
            endpoint,
            username,
            open_browser,
            typer.echo,
            session_main if foreground else None,
        )


@app.command("generate", help=message("text.generate_and_save_an_image"))
def generate_command(
    ctx: typer.Context,
    prompt: Annotated[
        str | None, typer.Option(help=message("text.description_of_the_image_to_generate"))
    ] = None,
    negative: Annotated[str | None, typer.Option(help=message("text.content_to_avoid"))] = None,
    seed: Annotated[
        int | None, typer.Option(min=0, max=2**64 - 1, help=message("text.random_seed"))
    ] = None,
    profile: Annotated[str | None, typer.Option(help=message("profile.select_help"))] = None,
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
            profile,
        )
        typer.echo(message("text.saved_directory", directory=run_dir(project, run.run_id)))


@app.command("collect", help=message("text.collect_completed_images_and_generation_history"))
def collect_command(
    ctx: typer.Context, run_id: Annotated[UUID | None, typer.Argument()] = None
) -> None:
    """Collect completed images and generation conditions."""
    with operation(ctx) as project, connected(project) as (client, session):
        result = collect(project, client, session, run_id)
        if capabilities(client):
            material_errors = MaterialCollector(project, client).collect()
            result = Collection(
                result.saved,
                result.pending,
                result.failed,
                (*result.errors, *material_errors),
                result.pending_ids,
            )
        report(result)


@app.command("open", help=message("text.open_a_comfyui_view_in_the_browser"))
def open_command(
    ctx: typer.Context,
    view: Annotated[Literal["app", "graph"], typer.Option(help=message("cli.view_help"))] = "app",
    profile: Annotated[str | None, typer.Option(help=message("profile.select_help"))] = None,
) -> None:
    """Open the selected ComfyUI view."""
    with operation(ctx) as project, connected(project) as (client, session):
        client.json("/system_stats")
        verify_runtime(client, session)
        open_view(project, session, view, profile, typer.echo)


@app.command(help=message("text.check_the_connection_gpu_model_and_required_nodes"))
def doctor(ctx: typer.Context) -> None:
    """Verify the connection, GPU, model and required nodes."""
    with operation(ctx) as project, connected(project) as (client, session):
        verify_runtime(client, session)
        stats = client.json("/system_stats")
        nodes = client.json("/object_info")
        settings = session.settings or project.settings()
        required: set[str] = set()
        for name in settings.profiles:
            _, profile = select(settings, name)
            graph, _ = prepare(
                profile.sample_prompt, None, 1, name, settings=settings, profile_name=name
            )
            required.update(n["class_type"] for n in graph.values())
        missing = required - nodes.keys()
        if missing:
            raise KomaquillError(
                display_key="text.required_standard_nodes_are_missing_check_the_comfyui"
            )
        loaders = {
            "checkpoints": ("CheckpointLoaderSimple", "ckpt_name"),
            "loras": ("LoraLoaderModelOnly", "lora_name"),
            "upscale_models": ("UpscaleModelLoader", "model_name"),
        }
        for asset in assets(settings):
            loader, field = loaders[asset.role]
            choices = input_choices(nodes, loader, field)
            if asset.reference not in choices:
                raise KomaquillError(
                    display_key="text.the_selected_model_is_missing_check_the_comfyui"
                )
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
            except KomaquillError:
                typer.echo(message("profile.colab_unknown"))
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
        monitor = read_monitor(project)
        if monitor is not None:
            typer.echo(
                message(
                    "monitor.status",
                    phase=monitor.phase
                    if alive(monitor) or monitor.phase in {"stopped", "failed"}
                    else "unknown",
                    observed=monitor.observed_at.isoformat(),
                )
            )
        preparation_count = sum(
            Preparation.model_validate_json(path.read_bytes()).phase
            not in {"ready", "cancelled", "failed"}
            for path in (project.state / "preparation").glob("*.json")
        )
        material_count = sum(
            Material.model_validate_json(path.read_bytes()).state == "pending"
            for path in (project.state / "preservation").glob("*.json")
        )
        typer.echo(message("assets.status", count=preparation_count))
        typer.echo(message("materials.status", count=material_count))
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
        if not supervisor.stop(project, force, typer.echo):
            runtime.stop(project, force, typer.echo)


@app.command("logs", help=message("monitor.logs_help"))
def logs_command(
    ctx: typer.Context,
    follow: Annotated[bool, typer.Option(help=message("monitor.follow_help"))] = False,
) -> None:
    """Render allowlisted event fields from retained JSON Lines."""
    with operation(ctx) as project:
        try:
            for event in supervisor.events(project, follow):
                failure = None
                if isinstance(event.get("failure"), dict):
                    with suppress(ValidationError):
                        failure = FailureInfo.model_validate(event["failure"])
                typer.echo(
                    message(
                        "monitor.log",
                        at=event.get("created_at", "-"),
                        operation=event.get("operation", "-"),
                        result=event.get("result", "-"),
                        identity=event.get("run_id") or event.get("asset") or "",
                        failure=f"{failure.code}/{failure.stage}" if failure else "",
                    )
                )
        except KeyboardInterrupt:
            return


@app.command("prepare", help=message("assets.prepare_help"))
def prepare_command(
    ctx: typer.Context,
    workflow: Annotated[
        Path, typer.Argument(help=message("assets.workflow_help"), exists=True, dir_okay=False)
    ],
) -> None:
    """Prepare explicitly confirmed public models on the current connection."""
    from komaquill.infrastructure.preparation import prepare_workflow

    with operation(ctx) as project, connected(project) as (client, session):
        prepare_workflow(
            project.configured(session.settings), client, workflow, typer.echo, typer.confirm
        )


@app.command("clean", help=message("cleanup.help"))
def clean_command(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run/--no-dry-run", help=message("cleanup.preview_help"))
    ] = False,
) -> None:
    """Preview or remove only eligible recreatable directories."""
    from komaquill.infrastructure.cleanup import execute

    with operation(ctx) as project:
        items, report_path, failed = execute(project, dry_run)
        for item in items:
            typer.echo(
                message(
                    "cleanup.item",
                    path=item.relative_path,
                    size=item.size_bytes,
                    result=message("cleanup." + (item.result or item.reason or "eligible")),
                )
            )
        if report_path is not None:
            typer.echo(message("cleanup.report", path=report_path))
        if failed:
            raise typer.Exit(1)
