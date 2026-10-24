"""Expose story planning and progressive rendering through the terminal."""

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from komaquill.application.generation import verify_runtime
from komaquill.application.stories.planning import plan_story
from komaquill.application.stories.rendering import create_render, render_story, story_view
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import JsonObject, Session
from komaquill.domain.stories import StoryInput, StoryPlan
from komaquill.infrastructure.comfyui import ComfyClient
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.sessions.connection import connected
from komaquill.infrastructure.stories.configuration import StorySettings, settings
from komaquill.infrastructure.stories.llm import planner
from komaquill.infrastructure.stories.storage import FileStoryRepository
from komaquill.messages import message

GROUP = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=message("stories.help"),
)


def source_input(file: Path, options: StorySettings) -> StoryInput:
    text = file.read_bytes().decode("utf-8-sig")
    return StoryInput(
        source_name=file.name,
        source_text=text,
        source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        image_count=options.images,
        purpose="verification"
        if options.llm_profiles[options.llm_profile].provider == "fake"
        else options.purpose,
        case_id=options.case_id,
    )


def make_plan(
    project: Project,
    repository: FileStoryRepository,
    file: Path,
    options: StorySettings,
    allow_real_llm: bool,
    json_output: bool,
) -> StoryPlan:
    request = source_input(file, options)
    if request.image_count > options.max_images:
        raise KomaquillError(code="invalid_story_count", stage="story_planning", retryable=False)
    model = planner(project, options, allow_real_llm=allow_real_llm)
    typer.echo(
        message(
            "stories.planning", story_id=request.story_id, model=model.provenance.configured_model
        ),
        err=True,
    )
    record_event(project, "story_planning", "started", story_id=request.story_id)
    started = time.monotonic()
    try:
        result = plan_story(
            repository,
            model,
            request,
            allow_real_llm=allow_real_llm,
            repair_attempts=options.repair_attempts,
            max_images=options.max_images,
        )
    except KeyboardInterrupt:
        typer.echo(message("stories.planning_interrupted", story_id=request.story_id), err=True)
        raise typer.Exit(130) from None
    except Exception:
        record_event(
            project,
            "story_planning",
            "failed",
            story_id=request.story_id,
            elapsed_seconds=time.monotonic() - started,
        )
        raise
    record_event(
        project,
        "story_planning",
        "saved",
        story_id=request.story_id,
        elapsed_seconds=time.monotonic() - started,
    )
    return result


def display(project: Project, view: JsonObject) -> None:
    typer.echo(message("stories.heading", title=view["title"], story_id=view["story_id"]))
    for scene in view["scenes"]:
        typer.echo(
            message(
                "stories.scene",
                position=scene["position"],
                total=view["image_count"],
                title=scene["title"],
                state=scene["state"],
                narrative=scene["narrative"],
            )
        )
        if scene["primary_image"]:
            typer.echo(
                message(
                    "stories.image",
                    path=project.artifacts_store.path(scene["primary_image"]["key"]),
                )
            )
    if view["render_id"]:
        typer.echo(message("stories.render_identity", render_id=view["render_id"]))


def execute_render(
    project: Project,
    repository: FileStoryRepository,
    client: ComfyClient,
    session: Session,
    render_id: UUID,
    retry_failed: bool,
    json_output: bool,
) -> None:
    typer.echo(message("stories.render_identity", render_id=render_id), err=json_output)
    printed: set[tuple[str, str | None, str]] = set()

    def progress(view: JsonObject) -> None:
        for scene in view["scenes"]:
            identity = (scene["scene_id"], scene["run_id"], scene["state"])
            if identity in printed or scene["state"] == "unstarted":
                continue
            printed.add(identity)
            record_event(
                project,
                "story_rendering",
                scene["state"],
                story_id=UUID(view["story_id"]),
                render_id=render_id,
                scene_id=UUID(scene["scene_id"]),
                run_id=UUID(scene["run_id"]) if scene["run_id"] else None,
            )
            if scene["state"] == "saved":
                typer.echo(
                    message(
                        "stories.scene",
                        position=scene["position"],
                        total=view["image_count"],
                        title=scene["title"],
                        state=scene["state"],
                        narrative=scene["narrative"],
                    ),
                    err=json_output,
                )
                if scene["primary_image"]:
                    typer.echo(
                        message(
                            "stories.image",
                            path=project.artifacts_store.path(scene["primary_image"]["key"]),
                        ),
                        err=json_output,
                    )
            else:
                typer.echo(
                    message(
                        "stories.progress",
                        position=scene["position"],
                        total=view["image_count"],
                        state=scene["state"],
                    ),
                    err=True,
                )
            if scene["failure"]:
                typer.echo(
                    message(
                        "stories.scene_failure",
                        position=scene["position"],
                        code=scene["failure"]["code"],
                    ),
                    err=True,
                )

    try:
        value = render_story(
            repository,
            project,
            client,
            session,
            render_id,
            retry_failed=retry_failed,
            notify=progress,
        )
    except KeyboardInterrupt:
        typer.echo(message("stories.interrupted", render_id=render_id), err=True)
        raise typer.Exit(130) from None
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=False))
    if not value["complete"]:
        typer.echo(message("stories.incomplete", render_id=render_id), err=True)
        raise typer.Exit(1)
    typer.echo(message("stories.complete", render_id=render_id), err=json_output)


def bind(
    app: typer.Typer,
    operation: Callable[[typer.Context], AbstractContextManager[Project]],
) -> None:
    """Share the CLI error boundary while keeping optional SDK imports lazy."""
    app.add_typer(GROUP, name="stories")

    @contextmanager
    def story_operation(ctx: typer.Context) -> Iterator[Project]:
        with operation(ctx) as project:
            try:
                yield project
            except (UnicodeError, ValueError, KeyError):
                typer.echo(message("stories.invalid_data"), err=True)
                raise typer.Exit(1) from None

    @GROUP.command("plan", help=message("stories.plan_help"))
    def plan_command(
        ctx: typer.Context,
        file: Annotated[
            Path, typer.Argument(help=message("stories.file_help"), exists=True, dir_okay=False)
        ],
        images: Annotated[
            int | None, typer.Option(min=1, help=message("stories.images_help"))
        ] = None,
        llm_profile: Annotated[
            str | None, typer.Option(help=message("stories.llm_profile_help"))
        ] = None,
        allow_real_llm: Annotated[
            bool, typer.Option(help=message("stories.allow_real_help"))
        ] = False,
        json_output: Annotated[
            bool, typer.Option("--json", help=message("stories.json_help"))
        ] = False,
    ) -> None:
        with story_operation(ctx) as project:
            options = settings(project, images=images, llm_profile=llm_profile)
            repository = FileStoryRepository(project)
            plan = make_plan(project, repository, file, options, allow_real_llm, json_output)
            value = story_view(repository, project, plan.request.story_id)
            if json_output:
                typer.echo(json.dumps(value, ensure_ascii=False))
            else:
                display(project, value)

    @GROUP.command("generate", help=message("stories.generate_help"))
    def generate_command(
        ctx: typer.Context,
        file: Annotated[
            Path, typer.Argument(help=message("stories.file_help"), exists=True, dir_okay=False)
        ],
        images: Annotated[
            int | None, typer.Option(min=1, help=message("stories.images_help"))
        ] = None,
        llm_profile: Annotated[
            str | None, typer.Option(help=message("stories.llm_profile_help"))
        ] = None,
        image_profile: Annotated[
            str | None, typer.Option(help=message("stories.image_profile_help"))
        ] = None,
        allow_real_llm: Annotated[
            bool, typer.Option(help=message("stories.allow_real_help"))
        ] = False,
        seed: Annotated[
            int | None, typer.Option(min=0, max=2**64 - 1, help=message("stories.seed_help"))
        ] = None,
        json_output: Annotated[
            bool, typer.Option("--json", help=message("stories.json_help"))
        ] = False,
    ) -> None:
        with story_operation(ctx) as project:
            options = settings(
                project, images=images, llm_profile=llm_profile, image_profile=image_profile
            )
            # Permission is checked before opening model or image connections.
            model_profile = options.llm_profiles[options.llm_profile]
            if model_profile.provider != "fake" and not allow_real_llm:
                raise KomaquillError(
                    code="real_llm_not_allowed", stage="story_planning", retryable=False
                )
            with connected(project) as (client, session):
                verify_runtime(client, session)
                stats = client.json("/system_stats")
                devices = stats.get("devices", [])
                if not isinstance(devices, list) or not any(
                    isinstance(device, dict)
                    and device.get("type")
                    and str(device["type"]).lower() != "cpu"
                    for device in devices
                ):
                    raise KomaquillError(code="image_backend_unavailable", stage="story_rendering")
                if (
                    session.settings is None
                    or options.image_profile not in session.settings.profiles
                ):
                    raise KomaquillError(code="invalid_configuration", stage="story_rendering")
                repository = FileStoryRepository(project)
                plan = make_plan(project, repository, file, options, allow_real_llm, json_output)
                render = create_render(
                    repository, project, session, plan.request.story_id, options.image_profile, seed
                )
                execute_render(
                    project, repository, client, session, render.render_id, False, json_output
                )

    @GROUP.command("render", help=message("stories.render_help"))
    def render_command(
        ctx: typer.Context,
        story_id: UUID,
        image_profile: Annotated[
            str | None, typer.Option(help=message("stories.image_profile_help"))
        ] = None,
        seed: Annotated[
            int | None, typer.Option(min=0, max=2**64 - 1, help=message("stories.seed_help"))
        ] = None,
        json_output: Annotated[
            bool, typer.Option("--json", help=message("stories.json_help"))
        ] = False,
    ) -> None:
        with story_operation(ctx) as project, connected(project) as (client, session):
            repository = FileStoryRepository(project)
            profile = image_profile or settings(project).image_profile
            render = create_render(repository, project, session, story_id, profile, seed)
            execute_render(
                project, repository, client, session, render.render_id, False, json_output
            )

    @GROUP.command("resume", help=message("stories.resume_help"))
    def resume_command(
        ctx: typer.Context,
        render_id: UUID,
        retry_failed: Annotated[bool, typer.Option(help=message("stories.retry_help"))] = False,
        json_output: Annotated[
            bool, typer.Option("--json", help=message("stories.json_help"))
        ] = False,
    ) -> None:
        with story_operation(ctx) as project, connected(project) as (client, session):
            execute_render(
                project,
                FileStoryRepository(project),
                client,
                session,
                render_id,
                retry_failed,
                json_output,
            )

    @GROUP.command("show", help=message("stories.show_help"))
    def show_command(
        ctx: typer.Context,
        story_id: UUID,
        render_id: Annotated[
            UUID | None, typer.Option(help=message("stories.render_id_help"))
        ] = None,
        follow: Annotated[bool, typer.Option(help=message("stories.follow_help"))] = False,
        json_output: Annotated[
            bool, typer.Option("--json", help=message("stories.json_help"))
        ] = False,
    ) -> None:
        with story_operation(ctx) as project:
            previous = None
            try:
                while True:
                    value = story_view(FileStoryRepository(project), project, story_id, render_id)
                    encoded = json.dumps(value, ensure_ascii=False)
                    if encoded != previous:
                        if json_output:
                            typer.echo(encoded)
                        else:
                            display(project, value)
                        previous = encoded
                    if not follow or value["complete"]:
                        return
                    time.sleep(project.settings().poll_interval)
            except KeyboardInterrupt:
                raise typer.Exit(130) from None
