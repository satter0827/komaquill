"""Compose local feature services without starting external resources."""

import json
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from komaquill.library.service import Images as LibraryImages
    from komaquill.library.service import LibraryService

from komaquill.adapters.database import ConversationRepository, database
from komaquill.adapters.llm.conversation import ConversationResponder
from komaquill.adapters.llm.creation import DraftGenerator
from komaquill.adapters.llm.execution import StructuredExecution
from komaquill.adapters.llm.images import ImageDescriber
from komaquill.conversations.service import ConversationService


def conversations(workspace: Path, execution: StructuredExecution) -> ConversationService:
    storage = database(workspace / ".artifacts/data/komaquill.sqlite3")
    repository = ConversationRepository(storage)
    return ConversationService(repository, ConversationResponder(execution, repository.save_memory))


def library(
    workspace: Path,
    conversations_service: ConversationService,
    images: "LibraryImages",
    execution: StructuredExecution,
) -> "LibraryService":
    from komaquill.adapters.library_repository import LibraryRepository
    from komaquill.library.service import LibraryService

    root = files("komaquill").joinpath("resources/library")
    return LibraryService(
        LibraryRepository(database(workspace / ".artifacts/data/komaquill.sqlite3")),
        conversations_service,
        DraftGenerator(execution),
        ImageDescriber(execution),
        images,
        json.loads(root.joinpath("samples.json").read_bytes()),
        tomllib.loads(root.joinpath("defaults.toml").read_text(encoding="utf-8")),
    )
