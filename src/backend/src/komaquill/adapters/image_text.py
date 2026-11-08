"""Freeze source descriptions and their English conditioning before image submission."""

import hashlib
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict

from komaquill.adapters.artifacts import LocalArtifactStore
from komaquill.adapters.llm.language import JAPANESE_TEXT
from komaquill.adapters.paths import WorkspacePaths
from komaquill.adapters.storage_errors import stored
from komaquill.application.library.service import ImageDescriber
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
    def __init__(self, workspace: Path, describer: ImageDescriber) -> None:
        self.workspace = workspace
        self.describer = describer

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
        store = LocalArtifactStore(WorkspacePaths(self.workspace))
        key = f"image-preparations/{identity}.json"
        if store.repository.reference(key) is not None:
            value = PreparedDescriptions.model_validate_json(store.read(key))
            if value.fingerprint != fingerprint:
                raise KomaquillError(code="operation_conflict", retryable=False)
            return value
        sources: list[PromptSource] = []
        roles = {
            (
                "description"
                if index == 0
                else "negative"
                if index == len(values) - 1
                else f"direction_{index}"
            ): original
            for index, original in enumerate(values)
        }
        if sum(bool(JAPANESE_TEXT.search(value)) for value in values) > 1:
            translated = list(
                self.describer.translate_fields(
                    roles, uuid5(identity, "descriptions"), profile, permitted
                ).values()
            )
        else:
            translated = [
                self.describer.prepare_image(
                    original, uuid5(identity, f"description:{index}"), profile, permitted
                )
                for index, original in enumerate(values)
            ]
        for index, original in enumerate(values):
            operation = uuid5(identity, f"description:{index}")
            english = translated[index]
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
        store.write(key, result.model_dump_json().encode())
        return result
