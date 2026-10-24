"""Parse story commands and present shared application results."""

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from komaquill.api import Client, KomaquillError, StoryEvent, StoryInterrupted
from komaquill.domain.models import JsonObject
from komaquill.infrastructure.configuration import Project
from komaquill.messages import message

GROUP = typer.Typer(no_args_is_help=True, add_completion=False, help=message("stories.help"))


def display(client: Client, view: JsonObject) -> None:
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
                    path=client.artifact_path(scene["primary_image"]["key"]),
                )
            )
    if view["render_id"]:
        typer.echo(message("stories.render_identity", render_id=view["render_id"]))


def present(client: Client, events: Iterator[StoryEvent], json_output: bool) -> None:
    """Write saved scenes once and keep diagnostics off structured output."""
    printed: set[tuple[UUID, UUID | None, str]] = set()
    last = None
    identity = None
    for event in events:
        last = event.snapshot
        if identity != last.render_id:
            identity = last.render_id
            typer.echo(message("stories.render_identity", render_id=identity), err=json_output)
        for scene in last.scenes:
            key = (scene.scene_id, scene.run_id, scene.state)
            if key in printed or scene.state == "unstarted":
                continue
            printed.add(key)
            if scene.state == "saved":
                typer.echo(
                    message(
                        "stories.scene",
                        position=scene.position,
                        total=last.image_count,
                        title=scene.title,
                        state=scene.state,
                        narrative=scene.narrative,
                    ),
                    err=json_output,
                )
                if scene.primary_image:
                    typer.echo(
                        message(
                            "stories.image", path=client.artifact_path(scene.primary_image.key)
                        ),
                        err=json_output,
                    )
            else:
                typer.echo(
                    message(
                        "stories.progress",
                        position=scene.position,
                        total=last.image_count,
                        state=scene.state,
                    ),
                    err=True,
                )
            if scene.failure:
                typer.echo(
                    message(
                        "stories.scene_failure", position=scene.position, code=scene.failure.code
                    ),
                    err=True,
                )
    if last is None:
        raise KomaquillError(code="internal_error", stage="story_rendering")
    if json_output:
        typer.echo(last.model_dump_json())
    if not last.complete:
        typer.echo(message("stories.incomplete", render_id=last.render_id), err=True)
        raise typer.Exit(1)
    typer.echo(message("stories.complete", render_id=last.render_id), err=json_output)


def bind(
    app: typer.Typer, operation: Callable[[typer.Context], AbstractContextManager[Project]]
) -> None:
    """Keep terminal-only input and error handling outside the public API."""
    app.add_typer(GROUP, name="stories")

    @contextmanager
    def story_operation(ctx: typer.Context) -> Iterator[Client]:
        with operation(ctx) as project:
            try:
                yield Client(project.root)
            except KeyboardInterrupt as error:
                if isinstance(error, StoryInterrupted) and error.story_id:
                    typer.echo(
                        message("stories.planning_interrupted", story_id=error.story_id), err=True
                    )
                elif isinstance(error, StoryInterrupted) and error.render_id:
                    typer.echo(message("stories.interrupted", render_id=error.render_id), err=True)
                else:
                    typer.echo(message("stories.operation_interrupted"), err=True)
                raise typer.Exit(130) from None
            except KomaquillError as error:
                if "render_id" in error.details:
                    typer.echo(
                        message("stories.render_identity", render_id=error.details["render_id"]),
                        err=True,
                    )
                    typer.echo(
                        message("stories.incomplete", render_id=error.details["render_id"]),
                        err=True,
                    )
                raise
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
        image_profile: Annotated[
            str | None, typer.Option(help=message("stories.image_profile_help"))
        ] = None,
        allow_real_llm: Annotated[
            bool, typer.Option(help=message("stories.allow_real_help"))
        ] = False,
        json_output: Annotated[
            bool, typer.Option("--json", help=message("stories.json_help"))
        ] = False,
    ) -> None:
        with story_operation(ctx) as client:
            typer.echo(message("stories.planning_started"), err=True)
            plan = client.stories.plan(
                file.read_bytes().decode("utf-8-sig"),
                source_name=file.name,
                images=images,
                llm_profile=llm_profile,
                image_profile=image_profile,
                allow_real_llm=allow_real_llm,
            )
            value = client.stories.export(plan.request.story_id)
            if json_output:
                typer.echo(json.dumps(value, ensure_ascii=False))
            else:
                display(client, value)

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
        with story_operation(ctx) as client:
            present(
                client,
                client.stories.generate(
                    file.read_bytes().decode("utf-8-sig"),
                    source_name=file.name,
                    images=images,
                    llm_profile=llm_profile,
                    image_profile=image_profile,
                    seed=seed,
                    allow_real_llm=allow_real_llm,
                ),
                json_output,
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
        with story_operation(ctx) as client:
            present(
                client,
                client.stories.render(story_id, image_profile=image_profile, seed=seed),
                json_output,
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
        with story_operation(ctx) as client:
            present(
                client, client.stories.resume(render_id, retry_failed=retry_failed), json_output
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
        with story_operation(ctx) as client:
            values = (
                client.stories.follow(story_id, render_id=render_id)
                if follow
                else iter([client.stories.show(story_id, render_id=render_id)])
            )
            for value in values:
                if json_output:
                    typer.echo(value.model_dump_json())
                else:
                    display(client, value.model_dump(mode="json"))
