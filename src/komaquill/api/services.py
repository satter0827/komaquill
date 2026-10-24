"""Compose image, session and evaluation use cases for Python and terminal callers."""

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import UUID

from komaquill.application.evaluations import register
from komaquill.application.generation import Collection, collect, generate
from komaquill.domain.evaluations import Evaluation
from komaquill.domain.models import JsonObject, Run
from komaquill.domain.status import SessionStatus
from komaquill.infrastructure import inspection, supervisor
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import operation_scope
from komaquill.infrastructure.evaluations import FileEvaluationRepository, export_evidence, select
from komaquill.infrastructure.sessions import shutdown
from komaquill.infrastructure.sessions.connection import connected


class ImageService:
    def __init__(self, project: Project) -> None:
        self._project = project

    def generate(
        self,
        prompt: str,
        *,
        negative: str | None = None,
        seed: int | None = None,
        profile: str | None = None,
    ) -> Run:
        with operation_scope(), connected(self._project) as (backend, session):
            return generate(
                self._project,
                backend,
                session,
                prompt,
                negative,
                seed if seed is not None else secrets.randbits(63),
                profile,
            )

    def collect(self, run_id: UUID | None = None) -> Collection:
        with operation_scope(), connected(self._project) as (backend, session):
            return collect(self._project, backend, session, run_id)


class SessionService:
    def __init__(self, project: Project) -> None:
        self._project = project

    def status(self) -> SessionStatus:
        return inspection.inspect(self._project)

    def start(
        self,
        *,
        backend: Literal["colab", "external"] = "colab",
        endpoint: str | None = None,
        username: str | None = None,
        progress: Callable[[str], None] | None = None,
        interactive_auth: bool = False,
        foreground: Callable[[Path], int] | None = None,
    ) -> SessionStatus:
        with operation_scope():
            supervisor.start(
                self._project,
                backend,
                endpoint,
                username,
                False,
                progress or (lambda _: None),
                foreground,
                interactive_auth=interactive_auth,
            )
            return self.status()

    def stop(
        self, *, force: bool = False, progress: Callable[[str], None] | None = None
    ) -> SessionStatus:
        echo = progress or (lambda _: None)
        with operation_scope():
            if not supervisor.stop(self._project, force, echo):
                shutdown.stop(self._project, force, echo)
            return self.status()


class EvaluationService:
    def __init__(self, project: Project) -> None:
        self._project = project

    def add(self, records: list[Evaluation]) -> list[Evaluation]:
        register(self._project.repository, FileEvaluationRepository(self._project), records)
        return records

    def list(self, run_id: UUID | None = None) -> list[Evaluation]:
        return select(self._project, run_id)

    def export(self, run_id: UUID | None = None) -> JsonObject:
        return export_evidence(self._project, run_id)
