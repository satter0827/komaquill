"""Compose local feature services without starting external resources."""

from pathlib import Path

from komaquill.adapters.conversation_llm import StructuredResponder
from komaquill.adapters.database import ConversationRepository, database
from komaquill.conversations.service import ConversationService, Images


def conversations(workspace: Path, images: Images) -> ConversationService:
    storage = database(workspace / ".artifacts/data/komaquill.sqlite3")
    return ConversationService(
        ConversationRepository(storage), StructuredResponder(workspace), images
    )
