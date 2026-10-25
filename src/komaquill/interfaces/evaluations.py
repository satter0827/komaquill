"""Expose evaluation recording and evidence export through the public CLI."""

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import typer

from komaquill.api import Client
from komaquill.domain.errors import KomaquillError
from komaquill.domain.evaluations import Evaluation, Evaluator
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.evaluations import (
    read_import,
    write_export,
)
from komaquill.messages import message

GROUP = typer.Typer(no_args_is_help=True, add_completion=False, help=message("evaluations.help"))


def bind(
    app: typer.Typer, operation: Callable[[typer.Context], AbstractContextManager[Project]]
) -> None:
    """Reuse the CLI's safe error boundary without introducing a second application."""
    app.add_typer(GROUP, name="evaluations")

    def commit(project: Project, item: Evaluation) -> None:
        Client(project.root).evaluations.add([item])
        typer.echo(message("evaluations.saved", identity=item.evaluation_id))

    @GROUP.command("add", help=message("evaluations.add"))
    def add(
        ctx: typer.Context,
        run_id: UUID,
        decision: Annotated[Literal["accept", "reject", "undecided"], typer.Option()],
        criterion: Annotated[str, typer.Option()],
        reason: Annotated[str, typer.Option()],
        criterion_version: str = "1",
        evaluator: str = "user",
        image: str | None = None,
        supersedes: UUID | None = None,
        case_id: str | None = None,
        ai_model: str | None = None,
        model_version: str | None = None,
        instructions: Path | None = None,
    ) -> None:
        """Record one explicit adoption decision."""
        with operation(ctx) as project:
            reviewer = Evaluator(
                kind="ai" if ai_model else "human",
                name=evaluator,
                model=ai_model,
                model_version=model_version,
                instruction=instructions.read_bytes().decode("utf-8") if instructions else None,
            )
            item = Evaluation(
                run_id=run_id,
                decision=decision,
                criterion=criterion,
                reason=reason,
                criterion_version=criterion_version,
                evaluator=reviewer,
                image=image,
                supersedes=supersedes,
                case_id=case_id,
            )
            commit(project, item)

    @GROUP.command("compare", help=message("evaluations.compare"))
    def compare(
        ctx: typer.Context,
        run_a: UUID,
        run_b: UUID,
        preference: Annotated[Literal["first", "second", "equal", "undecided"], typer.Option()],
        criterion: Annotated[str, typer.Option()],
        reason: Annotated[str, typer.Option()],
        criterion_version: str = "1",
        evaluator: str = "user",
        supersedes: UUID | None = None,
        case_id: str | None = None,
        ai_model: str | None = None,
        model_version: str | None = None,
        instructions: Path | None = None,
    ) -> None:
        """Record pairwise preference independently of adoption."""
        with operation(ctx) as project:
            commit(
                project,
                Evaluation(
                    run_id=run_a,
                    comparison_run_id=run_b,
                    preference=preference,
                    criterion=criterion,
                    reason=reason,
                    criterion_version=criterion_version,
                    evaluator=Evaluator(
                        kind="ai" if ai_model else "human",
                        name=evaluator,
                        model=ai_model,
                        model_version=model_version,
                        instruction=instructions.read_bytes().decode("utf-8")
                        if instructions
                        else None,
                    ),
                    supersedes=supersedes,
                    case_id=case_id,
                ),
            )

    @GROUP.command("import", help=message("evaluations.import"))
    def import_records(ctx: typer.Context, file: Path) -> None:
        with operation(ctx) as project:
            Client(project.root).evaluations.add(read_import(file))
            typer.echo(message("evaluations.imported"))

    @GROUP.command("list", help=message("evaluations.list"))
    def list_records(ctx: typer.Context, run_id: UUID | None = None) -> None:
        with operation(ctx) as project:
            typer.echo(
                json.dumps(
                    [
                        row.model_dump(mode="json")
                        for row in Client(project.root).evaluations.list(run_id)
                    ]
                )
            )

    @GROUP.command("export", help=message("evaluations.export"))
    def export(
        ctx: typer.Context, output: Annotated[Path, typer.Option()], run_id: UUID | None = None
    ) -> None:
        with operation(ctx) as project:
            if output.exists():
                raise KomaquillError(code="invalid_evaluation", stage="evaluation")
            payload = Client(project.root).evaluations.export(run_id)
            write_export(output, payload)
            typer.echo(message("evaluations.exported", path=output))
