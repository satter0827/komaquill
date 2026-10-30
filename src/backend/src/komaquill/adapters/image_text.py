"""Freeze source descriptions and their English conditioning before image submission."""

import hashlib
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict

from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.conversation_llm import JAPANESE_TEXT, StructuredResponder
from komaquill.adapters.storage_errors import stored
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import PromptSource
from komaquill.domain.workflow import digest


class PreparedDescriptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fingerprint: str
    prompt: str
    subjects: tuple[str, ...]
    negative: str | None
    sources: tuple[PromptSource, ...]


class ImageTextPreparation:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.responder = StructuredResponder(workspace)

    @stored
    def prepare(
        self,
        identity: UUID,
        prompt: str,
        subjects: tuple[str, ...],
        negative: str | None,
        profile: str,
        permitted: bool,
    ) -> PreparedDescriptions:
        fingerprint = digest(
            {"prompt": prompt, "subjects": subjects, "negative": negative, "llm_profile": profile}
        )
        values = [prompt, *subjects, negative or ""]
        if not any(JAPANESE_TEXT.search(value) for value in values):
            return PreparedDescriptions(
                fingerprint=fingerprint,
                prompt=prompt,
                subjects=subjects,
                negative=negative,
                sources=(),
            )
        path = self.workspace / ".artifacts/data/image-preparations" / f"{identity}.json"
        if path.exists():
            value = PreparedDescriptions.model_validate_json(path.read_bytes())
            if value.fingerprint != fingerprint:
                raise KomaquillError(code="operation_conflict", retryable=False)
            return value
        sources: list[PromptSource] = []
        translated: list[str] = []
        for index, original in enumerate(values):
            operation = uuid5(identity, f"description:{index}")
            english = self.responder.prepare_image(original, operation, profile, permitted)
            translated.append(english)
            if english != original:
                role: Literal["negative", "positive"] = (
                    "negative" if index == len(values) - 1 else "positive"
                )
                for label, content in (("original", original), ("english", english)):
                    sources.append(
                        PromptSource(
                            name=f"{operation}-{label}.txt",
                            content=content,
                            role=role,
                            sha256=hashlib.sha256(content.encode()).hexdigest(),
                        )
                    )
        result = PreparedDescriptions(
            fingerprint=fingerprint,
            prompt=translated[0],
            subjects=tuple(translated[1:-1]),
            negative=translated[-1] if negative is not None else None,
            sources=tuple(sources),
        )
        atomic_json(path, result.model_dump(mode="json"))
        return result
