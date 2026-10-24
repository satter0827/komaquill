"""Bind feature services to an explicitly selected local workspace."""

from functools import cached_property
from pathlib import Path
from types import TracebackType

from komaquill.api.services import EvaluationService, ImageService, SessionService
from komaquill.api.stories import StoryService
from komaquill.infrastructure.configuration import find_project, initialize


class Client:
    """Share application services while keeping GPU ownership explicit."""

    def __init__(self, workspace: str | Path) -> None:
        self._project = find_project(Path(workspace))

    @classmethod
    def initialize(cls, workspace: str | Path) -> "Client":
        """Create settings and storage without overwriting existing files."""
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
    def sessions(self) -> SessionService:
        return SessionService(self._project)

    @cached_property
    def evaluations(self) -> EvaluationService:
        return EvaluationService(self._project)

    def artifact_path(self, key: str) -> Path:
        """Resolve a portable key using the configured local storage adapter."""
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
