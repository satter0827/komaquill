"""Compose local feature services without starting external resources."""

import json
import tomllib
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from komaquill.domain.library.models import CreationRequest

if TYPE_CHECKING:
    from komaquill.application.library.service import Images as LibraryImages
    from komaquill.application.library.service import LibraryService

from komaquill.adapters.diagnostics import record_event
from komaquill.adapters.llm.conversation import ConversationResponder
from komaquill.adapters.llm.creation import DraftGenerator
from komaquill.adapters.llm.execution import StructuredExecution
from komaquill.adapters.llm.images import ImageDescriber
from komaquill.adapters.paths import WorkspacePaths
from komaquill.adapters.persistence.connection import database
from komaquill.adapters.persistence.conversations import ConversationRepository
from komaquill.application.conversations.service import ConversationService


def conversations(
    workspace: Path, execution: StructuredExecution, preflight: Callable[[UUID, bool], None]
) -> ConversationService:
    storage = database(workspace / ".artifacts/data/komaquill.sqlite3")
    repository = ConversationRepository(storage)
    return ConversationService(
        repository,
        ConversationResponder(execution, repository.save_memory, repository.history_hash),
        preflight,
        report_failure=lambda identity, failure: record_event(
            WorkspacePaths(workspace),
            "conversation",
            "failed",
            operation_id=identity,
            failure=failure,
        ),
    )


def library(
    workspace: Path,
    conversations_service: ConversationService,
    images: "LibraryImages",
    execution: StructuredExecution,
    preflight: Callable[[CreationRequest, str], None],
) -> "LibraryService":
    from komaquill.adapters.persistence.library import LibraryRepository
    from komaquill.application.library.service import LibraryService

    root = files("komaquill").joinpath("resources/library")
    return LibraryService(
        LibraryRepository(database(workspace / ".artifacts/data/komaquill.sqlite3")),
        conversations_service,
        DraftGenerator(execution),
        ImageDescriber(execution),
        images,
        json.loads(root.joinpath("samples.json").read_bytes()),
        tomllib.loads(root.joinpath("defaults.toml").read_text(encoding="utf-8")),
        image_lookup=images.read,
        preflight=preflight,
    )
