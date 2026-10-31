"""Compose local feature services without starting external resources."""

import json
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from komaquill.library.service import Images as LibraryImages
    from komaquill.library.service import LibraryService

from komaquill.adapters.conversation_llm import StructuredResponder
from komaquill.adapters.database import ConversationRepository, database
from komaquill.conversations.service import ConversationService


def conversations(workspace: Path) -> ConversationService:
    storage = database(workspace / ".artifacts/data/komaquill.sqlite3")
    return ConversationService(ConversationRepository(storage), StructuredResponder(workspace))


def library(
    workspace: Path, conversations_service: ConversationService, images: "LibraryImages"
) -> "LibraryService":
    from komaquill.adapters.library_repository import LibraryRepository
    from komaquill.library.service import LibraryService

    root = files("komaquill").joinpath("resources/library")
    return LibraryService(
        LibraryRepository(database(workspace / ".artifacts/data/komaquill.sqlite3")),
        conversations_service,
        cast(StructuredResponder, conversations_service.responder),
        images,
        json.loads(root.joinpath("samples.json").read_bytes()),
        tomllib.loads(root.joinpath("defaults.toml").read_text(encoding="utf-8")),
    )
