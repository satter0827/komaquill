"""Compose image, session and evaluation use cases for Python and terminal callers."""

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import UUID

from komaquill.api._errors import api_operation
from komaquill.application.evaluations import register
from komaquill.application.generation import Collection, collect, generate
from komaquill.domain.errors import ErrorCode, KomaquillError, OperationStage
from komaquill.domain.evaluations import Evaluation
from komaquill.domain.models import JsonObject, Run
from komaquill.domain.status import SessionStatus
from komaquill.infrastructure import inspection, supervisor
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import operation_scope
from komaquill.infrastructure.evaluations import FileEvaluationRepository, export_evidence, select
from komaquill.infrastructure.materials import MaterialCollector
from komaquill.infrastructure.preparation import capabilities
from komaquill.infrastructure.sessions import shutdown
from komaquill.infrastructure.sessions.connection import connected
from komaquill.infrastructure.stories.associations import story_associations
from komaquill.infrastructure.stories.publication import refresh_saved_stories


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
        with (
            operation_scope(),
            api_operation(OperationStage.GENERATION, invalid=ErrorCode.INVALID_CONFIGURATION),
            connected(self._project) as (backend, session),
        ):
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
        with operation_scope(), api_operation(OperationStage.COLLECTION):
            with connected(self._project) as (backend, session):
                result = collect(self._project, backend, session, run_id)
                if capabilities(backend):
                    material_errors = MaterialCollector(self._project, backend).collect()
                    result = Collection(
                        result.saved,
                        result.pending,
                        result.failed,
                        (*result.errors, *material_errors),
                        result.pending_ids,
                        observed_versions=result.observed_versions,
                    )
            publications = refresh_saved_stories(self._project)
            if publications:
                raise KomaquillError(
                    code="publication_failed",
                    stage="finalize",
                    details={"story_id": str(publications[0])},
                )
            return result


class SessionService:
    def __init__(self, project: Project) -> None:
        self._project = project

    def status(self) -> SessionStatus:
        with api_operation(OperationStage.CHECK):
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
        profiles: list[str] | None = None,
        open_browser: bool = False,
    ) -> SessionStatus:
        with (
            operation_scope(),
            api_operation(OperationStage.PREPARATION, invalid=ErrorCode.INVALID_CONFIGURATION),
        ):
            project = self._project
            if profiles is not None:
                settings = project.settings()
                settings = type(settings).model_validate(
                    {**settings.model_dump(), "profiles": profiles}
                )
                project = project.configured(settings)
            supervisor.start(
                project,
                backend,
                endpoint,
                username,
                open_browser,
                progress or (lambda _: None),
                foreground,
                interactive_auth=interactive_auth,
            )
            return self.status()

    def stop(
        self, *, force: bool = False, progress: Callable[[str], None] | None = None
    ) -> SessionStatus:
        echo = progress or (lambda _: None)
        with operation_scope(), api_operation(OperationStage.GPU_STOP):
            if not supervisor.stop(self._project, force, echo):
                shutdown.stop(self._project, force, echo)
            return self.status()


class EvaluationService:
    def __init__(self, project: Project) -> None:
        self._project = project

    def add(self, records: list[Evaluation]) -> list[Evaluation]:
        with api_operation(OperationStage.EVALUATION):
            register(self._project.repository, FileEvaluationRepository(self._project), records)
            return records

    def list(self, run_id: UUID | None = None) -> list[Evaluation]:
        with api_operation(OperationStage.EVALUATION):
            return select(self._project, run_id)

    def export(self, run_id: UUID | None = None) -> JsonObject:
        with api_operation(OperationStage.EVALUATION):
            payload = export_evidence(self._project, run_id)
            links, unreadable = story_associations(self._project)
            payload["unreadable_story_renders"] = unreadable
            for row in payload["runs"]:
                row["story_context"] = links.get(row["run"]["run_id"], [])
            return payload
