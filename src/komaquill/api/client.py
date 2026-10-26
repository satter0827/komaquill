"""Bind feature services to an explicitly selected local workspace."""

from functools import cached_property
from pathlib import Path
from types import TracebackType

from komaquill.api._errors import api_operation
from komaquill.api.services import EvaluationService, ImageService, InputService, SessionService
from komaquill.api.stories import StoryService
from komaquill.domain.errors import ErrorCode, OperationStage
from komaquill.domain.storage import MigrationReport
from komaquill.infrastructure.configuration import find_project, initialize


class Client:
    """Share application services while keeping GPU ownership explicit."""

    def __init__(self, workspace: str | Path) -> None:
        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            self._project = find_project(Path(workspace))

    @classmethod
    def initialize(cls, workspace: str | Path) -> "Client":
        """Create settings and storage without overwriting existing files."""
        with api_operation(OperationStage.PREPARATION, invalid=ErrorCode.INVALID_CONFIGURATION):
            initialize(Path(workspace))
            return cls(workspace)

    @property
    def workspace(self) -> Path:
        return self._project.root

    @cached_property
    def stories(self) -> StoryService:
        return StoryService(self._project)

    @cached_property
    def images(self) -> ImageService:
        return ImageService(self._project)

    @cached_property
    def inputs(self) -> InputService:
        return InputService(self._project)

    @cached_property
    def sessions(self) -> SessionService:
        return SessionService(self._project)

    @cached_property
    def evaluations(self) -> EvaluationService:
        return EvaluationService(self._project)

    def migrate(self, *, dry_run: bool = False) -> MigrationReport:
        """Migrate or rebuild local storage only when the session is stopped."""
        from filelock import Timeout

        from komaquill.domain.errors import KomaquillError
        from komaquill.infrastructure.migration import migrate

        try:
            return MigrationReport.model_validate(migrate(self._project, dry_run=dry_run))
        except Timeout:
            raise KomaquillError(code="invalid_request", stage="storage") from None
        except (OSError, ValueError):
            raise KomaquillError(code="storage_failed", stage="storage") from None

    def artifact_path(self, key: str) -> Path:
        """Resolve a portable key using the configured local storage adapter."""
        with api_operation(OperationStage.STORAGE):
            return self._project.artifacts_store.path(key)

    def __enter__(self) -> "Client":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave the independently managed supervisor and GPU running."""
