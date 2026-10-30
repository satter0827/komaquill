"""Bind explicit browser serving and conversation commands to the public client."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import wraps
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

import typer

from komaquill.api import Client, KomaquillError
from komaquill.conversations.models import ConversationInput, SendInput, StateCommand
from komaquill.messages import error_message, message


def bind(
    app: typer.Typer, operation: Callable[[typer.Context], AbstractContextManager[object]]
) -> None:
    def guarded(function: Callable[..., None]) -> Callable[..., None]:
        @wraps(function)
        def wrapped(ctx: typer.Context, *args: object, **kwargs: object) -> None:
            with operation(ctx):
                function(ctx, *args, **kwargs)

        return wrapped

    @app.command("web", help=message("web.help"))
    @guarded
    def web(
        ctx: typer.Context,
        port: Annotated[int, typer.Option(help=message("web.port"), min=1, max=65535)] = 8765,
        open_browser: Annotated[bool, typer.Option("--open", help=message("web.open"))] = False,
    ) -> None:
        try:
            import uvicorn

            from komaquill.interfaces.http.app import create_app
        except ImportError:
            typer.echo(message("web.dependencies"), err=True)
            raise typer.Exit(1) from None
        client = Client(ctx.obj or Path.cwd())
        if open_browser:
            import threading
            import webbrowser

            threading.Timer(1, webbrowser.open, args=(f"http://127.0.0.1:{port}",)).start()
        uvicorn.run(create_app(client), host="127.0.0.1", port=port, access_log=False)

    chat = typer.Typer(help=message("chat.help"), pretty_exceptions_enable=False)
    app.add_typer(chat, name="chat")

    @chat.command("create", help=message("chat.create"))
    @guarded
    def create(
        ctx: typer.Context,
        title: Annotated[str, typer.Argument(help=message("chat.title"))],
        profile: Annotated[str, typer.Option(help=message("chat.profile"))] = "fake",
        scenario: Annotated[str, typer.Option(help=message("chat.scenario"))] = "",
    ) -> None:
        result = Client(ctx.obj or Path.cwd()).conversations.create(
            ConversationInput(title=title, llm_profile=profile, scenario=scenario)
        )
        typer.echo(result.model_dump_json())

    @chat.command("list", help=message("chat.list"))
    @guarded
    def listing(ctx: typer.Context) -> None:
        for value in Client(ctx.obj or Path.cwd()).conversations.list():
            typer.echo(value.model_dump_json())

    @chat.command("get", help=message("chat.get"))
    @guarded
    def get(
        ctx: typer.Context, identity: Annotated[UUID, typer.Argument(help=message("chat.identity"))]
    ) -> None:
        typer.echo(Client(ctx.obj or Path.cwd()).conversations.get(identity).model_dump_json())

    @chat.command("send", help=message("chat.send"))
    @guarded
    def send(
        ctx: typer.Context,
        identity: Annotated[UUID, typer.Argument(help=message("chat.identity"))],
        text: Annotated[str, typer.Argument(help=message("chat.text"))],
        operation_id: Annotated[
            UUID | None, typer.Option(help=message("chat.operation_id"))
        ] = None,
        allow_real_llm: Annotated[bool, typer.Option(help=message("chat.allow_real"))] = False,
    ) -> None:
        client = Client(ctx.obj or Path.cwd())
        result = client.conversations.send(
            identity,
            SendInput(
                operation_id=operation_id or uuid4(), text=text, allow_real_llm=allow_real_llm
            ),
        )
        typer.echo(result.model_dump_json())
        if result.state == "complete":
            turn = next(
                turn
                for turn in client.conversations.get(identity).turns
                if turn.operation_id == result.id
            )
            typer.echo(turn.assistant)
        else:
            typer.echo(
                error_message(KomaquillError(code=result.error or "operation_failed")), err=True
            )
            raise typer.Exit(1)

    @chat.command("status", help=message("chat.status"))
    @guarded
    def status(
        ctx: typer.Context, identity: Annotated[UUID, typer.Argument(help=message("chat.identity"))]
    ) -> None:
        typer.echo(Client(ctx.obj or Path.cwd()).conversations.status(identity).model_dump_json())

    @chat.command("resume", help=message("chat.resume"))
    @guarded
    def resume(
        ctx: typer.Context, identity: Annotated[UUID, typer.Argument(help=message("chat.identity"))]
    ) -> None:
        typer.echo(Client(ctx.obj or Path.cwd()).conversations.resume(identity).model_dump_json())

    @chat.command("command", help=message("chat.command"))
    @guarded
    def command(
        ctx: typer.Context,
        identity: Annotated[UUID, typer.Argument(help=message("chat.identity"))],
        action: Annotated[
            Literal["reset_state", "set_state"], typer.Argument(help=message("chat.action"))
        ],
        expected_revision: Annotated[int, typer.Option(help=message("chat.revision"), min=0)],
        state: Annotated[str, typer.Option(help=message("chat.state"))] = "{}",
    ) -> None:
        import json

        request = StateCommand(
            command=action, expected_revision=expected_revision, state=json.loads(state)
        )
        typer.echo(
            Client(ctx.obj or Path.cwd()).conversations.command(identity, request).model_dump_json()
        )
