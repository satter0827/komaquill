"""Bind feature services to an explicitly selected local workspace."""

from functools import cached_property
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from komaquill.adapters.configuration import WorkspaceConfiguration
    from komaquill.adapters.llm.execution import StructuredExecution
    from komaquill.adapters.persistence.devices import DeviceStore
    from komaquill.adapters.persistence.works import WorkRepository
    from komaquill.adapters.web_configuration import WebSettings
    from komaquill.api.archive import ArchiveService
    from komaquill.api.availability import AvailabilityService
    from komaquill.application.conversations.service import ConversationService
    from komaquill.application.library.service import LibraryService, PlayerService, PresetService
    from komaquill.domain.library.models import (
        CharacterInput,
        CharacterPreset,
        StoryInput,
        StoryPreset,
    )
    from komaquill.domain.library.recovery import ImageLinkInspection, ImageLinkRepairResult

from komaquill.adapters.workspace import RuntimeWorkspace, find_project, initialize
from komaquill.api._errors import api_operation
from komaquill.api.evaluations import EvaluationService
from komaquill.api.images import ImageService, InputService
from komaquill.api.operations import OperationService
from komaquill.api.runtime import RuntimeService
from komaquill.domain.content import CONTENT_RATING
from komaquill.domain.errors import ErrorCode, OperationStage


class Client:
    """Share application services while keeping GPU ownership explicit."""

    def __init__(self, workspace: str | Path) -> None:
        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            project = find_project(Path(workspace))
            self._project = RuntimeWorkspace(
                project.root, configuration_source=lambda: self._configuration
            )

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
        from komaquill.adapters.persistence.migrate import migrate

        workspace = self.workspace
        self.close()
        result = migrate(workspace)
        # No service may retain a graph or connection to the replaced database.
        replacement = Client(workspace)
        self.__dict__.clear()
        self.__dict__.update(replacement.__dict__)
        return result

    def inspect_image_links(self) -> "ImageLinkInspection":
        """Record candidates without modifying the existing database."""
        from komaquill.adapters.image_link_repair import inspect

        return inspect(self.workspace)

    def repair_image_links(self, inspection_id: "UUID") -> "ImageLinkRepairResult":
        """Apply a retained inspection after verifying stopped storage and a backup."""
        from komaquill.adapters.image_link_repair import repair

        return repair(self.workspace, inspection_id)

    @cached_property
    def _configuration(self) -> "WorkspaceConfiguration":
        from komaquill.adapters.configuration import WorkspaceConfiguration

        return WorkspaceConfiguration(self.workspace)

    @cached_property
    def _execution(self) -> "StructuredExecution":
        from komaquill.adapters.llm.execution import StructuredExecution

        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            execution = StructuredExecution(self.workspace, lambda: self._configuration)
            binding = self.workspace / ".artifacts/state/llm-budget.json"
            if binding.exists():
                from komaquill.adapters.persistence.budgets import BudgetBinding, CallBudget
                from komaquill.adapters.persistence.connection import database

                value = BudgetBinding.model_validate_json(binding.read_bytes())
                execution.call_budget = CallBudget(
                    database(value.ledger), value.max_calls, value.max_seconds
                )
            return execution

    @cached_property
    def availability(self) -> "AvailabilityService":
        from komaquill.api.availability import AvailabilityService

        return AvailabilityService(
            self._project,
            self._execution,
            lambda identity: self.conversations.overview(identity),
            lambda request: self._library.source(request),
            lambda identity: self.images.read(identity),
            lambda identity: self.conversations.repository.operation_snapshot(identity),
            lambda: self._library.store.preferences().image_prompts,
            lambda: self.images.supports_negative(),
        )

    @cached_property
    def conversations(self) -> "ConversationService":
        from komaquill.composition.services import conversations

        return conversations(
            self.workspace, self._execution, self.availability.require_conversation
        )

    @cached_property
    def library(self) -> "LibraryService":
        service = self._library
        service.initialize()
        return service

    @cached_property
    def _library(self) -> "LibraryService":
        from komaquill.composition.services import library

        return library(
            self.workspace,
            self.conversations,
            self.images,
            self._execution,
            self.availability.require_creation,
        )

    @property
    def player(self) -> "PlayerService":
        return self.library.player

    @property
    def characters(self) -> "PresetService[CharacterInput, CharacterPreset]":
        return self.library.characters

    @property
    def stories(self) -> "PresetService[StoryInput, StoryPreset]":
        return self.library.stories

    @cached_property
    def operations(self) -> OperationService:
        return OperationService(self.conversations.repository)

    def limit_llm_calls(
        self, ledger: Path, *, max_calls: int, max_seconds: int, create: bool = False
    ) -> None:
        """Attach an allowance, requiring explicit creation for a new ledger."""
        from komaquill.adapters.atomic import atomic_json
        from komaquill.adapters.persistence.budgets import BudgetBinding, CallBudget
        from komaquill.adapters.persistence.connection import Database

        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_REQUEST):
            if type(create) is not bool:
                raise ValueError
            limits = BudgetBinding(ledger=ledger, max_calls=max_calls, max_seconds=max_seconds)
            ledger = limits.ledger.resolve()
            create = (
                create
                and not ledger.exists()
                and not (self.workspace / ".artifacts/state/llm-budget.json").exists()
            )
            self._execution.call_budget = CallBudget(
                Database(ledger, create=create), limits.max_calls, limits.max_seconds, create=create
            )
            atomic_json(
                self.workspace / ".artifacts/state/llm-budget.json",
                {
                    "ledger": str(ledger.resolve()),
                    "max_calls": limits.max_calls,
                    "max_seconds": limits.max_seconds,
                },
            )

    def llm_usage(self) -> dict[str, object]:
        """Read the attached allowance without contacting a model."""
        return self._execution.call_budget.snapshot() if self._execution.call_budget else {}

    def llm_records(self, operation: UUID) -> list[dict[str, Any]]:
        """Read private request evidence without credentials or HTTP headers."""
        with api_operation(OperationStage.STORAGE):
            return [value for _, value in self._execution.call_records(operation)]

    def operation_usage(self, operation: UUID) -> dict[str, Any]:
        with api_operation(OperationStage.STORAGE):
            return self._execution.usage(operation)

    @cached_property
    def devices(self) -> "DeviceStore":
        from komaquill.adapters.persistence.connection import database
        from komaquill.adapters.persistence.devices import DeviceStore

        return DeviceStore(
            database(self._project.data / "komaquill.sqlite3"), self._configuration.web
        )

    @property
    def web_settings(self) -> "WebSettings":
        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            return self._configuration.web

    def export_web_proxy(self, *, port: int = 8765) -> Path:
        from komaquill.adapters.web_proxy import write_config

        with api_operation(OperationStage.STORAGE):
            return write_config(
                self._project, self.web_settings, port, self._configuration.runtime.local_port
            )

    def close(self) -> None:
        if "_execution" in self.__dict__:
            self._execution.close()

    def export_conversation(
        self, identity: UUID, *, include_images: bool = True, include_conditions: bool = False
    ) -> Path:
        from komaquill.adapters.conversation_export import export_conversation
        from komaquill.domain.library.works import WorkRef

        with api_operation(OperationStage.STORAGE):
            conversation = self.conversations.get(identity)
            name = self.works.get(WorkRef(kind="conversation", id=identity)).name
            conversation = conversation.model_copy(
                update={"definition": conversation.definition.model_copy(update={"title": name})}
            )
            detached = self.works.detached("conversation", identity)
            assets = []
            if include_images or include_conditions:
                for job in self.library.conversation_images(identity):
                    if job.image_id is not None:
                        run = self.images.get(job.image_id)
                        visible = self.works.image_outputs([run])[run.run_id]
                        excluded = detached.get(f"job:{job.id}:{run.run_id}", [])
                        visible = [value for value in visible if value.index not in excluded]
                        if not visible:
                            continue
                        indices = {item.index for item in visible}
                        paths = (
                            [
                                self.artifact_path(f"runs/{run.run_id}/{name}")
                                if index in indices
                                else None
                                for index, name in enumerate(run.images)
                            ]
                            if include_images
                            else []
                        )
                        filenames = [run.images[item.index] for item in visible]
                        exported = run.model_copy(
                            update={
                                "images": filenames,
                                "image_metadata": {
                                    name: item
                                    for name, item in run.image_metadata.items()
                                    if name in filenames
                                },
                            }
                        )
                        assets.append((exported, paths))
            return export_conversation(
                self.workspace,
                conversation,
                CONTENT_RATING,
                assets,
                include_images=include_images,
                include_conditions=include_conditions,
            )

    @cached_property
    def runtime(self) -> RuntimeService:
        return RuntimeService(self._project, lambda: self.library.preferences().image_prompts)

    @cached_property
    def archive(self) -> "ArchiveService":
        from komaquill.api.archive import ArchiveService

        return ArchiveService(self._project)

    @cached_property
    def works(self) -> "WorkRepository":
        from komaquill.adapters.persistence.connection import database
        from komaquill.adapters.persistence.works import WorkRepository

        return WorkRepository(database(self._project.data / "komaquill.sqlite3"))

    @cached_property
    def images(self) -> ImageService:
        from komaquill.adapters.llm.images import ImageDescriber
        from komaquill.domain.availability import AvailabilityRequest

        return ImageService(
            self._project,
            ImageDescriber(self._execution),
            lambda prompt, subjects, negative, profile: self.availability.require(
                AvailabilityRequest(
                    kind="image",
                    prompt=prompt,
                    subjects=subjects,
                    negative=negative,
                    llm_profile=profile,
                )
            ),
        )

    def configure(self) -> "Client":
        """Freeze model and image settings for a long-lived local application."""
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
        self.close()
