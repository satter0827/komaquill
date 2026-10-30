"""Bind feature services to an explicitly selected local workspace."""

from functools import cached_property
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from komaquill.api.archive import ArchiveService
    from komaquill.conversations.service import ConversationService
    from komaquill.library.models import (
        CharacterInput,
        CharacterPreset,
        PlayerInput,
        PlayerPreset,
        StoryInput,
        StoryPreset,
    )
    from komaquill.library.service import LibraryService, PresetService

from komaquill.adapters.workspace import find_project, initialize
from komaquill.api._errors import api_operation
from komaquill.api.evaluations import EvaluationService
from komaquill.api.images import ImageService, InputService
from komaquill.api.operations import OperationService
from komaquill.api.runtime import RuntimeService
from komaquill.domain.errors import ErrorCode, OperationStage


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

    def migrate(self) -> Path:
        from komaquill.adapters.database_migration import migrate

        return migrate(self.workspace)

    @cached_property
    def conversations(self) -> "ConversationService":
        from komaquill.composition import conversations

        return conversations(self.workspace, self.images)

    @cached_property
    def library(self) -> "LibraryService":
        from komaquill.composition import library

        return library(self.workspace, self.conversations, self.images)

    @property
    def players(self) -> "PresetService[PlayerInput, PlayerPreset]":
        return self.library.players

    @property
    def characters(self) -> "PresetService[CharacterInput, CharacterPreset]":
        return self.library.characters

    @property
    def stories(self) -> "PresetService[StoryInput, StoryPreset]":
        return self.library.stories

    @cached_property
    def operations(self) -> OperationService:
        return OperationService(self.conversations.repository)

    @cached_property
    def runtime(self) -> RuntimeService:
        return RuntimeService(self._project)

    @cached_property
    def archive(self) -> "ArchiveService":
        from komaquill.api.archive import ArchiveService

        return ArchiveService(self._project)

    @cached_property
    def images(self) -> ImageService:
        return ImageService(self._project)

    def configure(self) -> "Client":
        """Freeze model and image settings for a long-lived local application."""
        self.conversations.profiles()
        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            self._project = self._project.configured()
        settings = self._project.settings()
        self.images.configure(settings)
        self.runtime.configure(settings)
        return self

    @cached_property
    def inputs(self) -> InputService:
        return InputService(self._project)

    @cached_property
    def evaluations(self) -> EvaluationService:
        return EvaluationService(self._project)

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
