"""Read portable prompt files at the terminal boundary."""

import json
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from komaquill.api import Client
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import ImageInput, PromptSource
from komaquill.domain.materials import InputArtifact
from komaquill.domain.rendering import RenderConfig


def read_source(path: Path, role: Literal["positive", "input"]) -> PromptSource:
    try:
        content = path.read_bytes()
        return PromptSource(
            name=path.name,
            content=content.decode("utf-8"),
            sha256=sha256(content).hexdigest(),
            role=role,
        )
    except (OSError, UnicodeError, ValueError):
        raise KomaquillError(
            code="invalid_request",
            stage="generation",
            display_key="inputs.invalid_file",
            retryable=False,
        ) from None


def image_input(
    prompt: str | None,
    prompt_file: Path | None = None,
    input_file: Path | None = None,
) -> tuple[ImageInput, tuple[PromptSource, ...]]:
    if input_file is not None:
        if any(value is not None for value in (prompt, prompt_file)):
            raise KomaquillError(
                code="invalid_request",
                stage="generation",
                display_key="inputs.json_conflict",
                retryable=False,
            )
        source = read_source(input_file, "input")
        try:
            value = ImageInput.model_validate_json(source.content.removeprefix("\ufeff"))
        except ValidationError:
            raise KomaquillError(
                code="invalid_request",
                stage="generation",
                display_key="inputs.invalid_json",
                retryable=False,
            ) from None
        return value, (source,)
    if prompt is not None and prompt_file is not None:
        raise KomaquillError(
            code="invalid_request",
            stage="generation",
            display_key="inputs.prompt_conflict",
            retryable=False,
        )
    sources: tuple[PromptSource, ...] = ()
    if prompt_file is not None:
        source = read_source(prompt_file, "positive")
        sources = (source,)
        prompt = source.content.removeprefix("\ufeff")
    try:
        value = ImageInput.model_validate({"prompt": prompt})
    except ValidationError:
        raise KomaquillError(
            code="invalid_request",
            stage="generation",
            display_key="inputs.invalid_prompt",
            retryable=False,
        ) from None
    return value, sources


def material_inputs(
    client: Client, profile_name: str | None, references: dict[str, str], base: Path
) -> dict[str, InputArtifact]:
    """Resolve user paths at the terminal boundary and retain only saved references."""
    if not references:
        return {}
    from komaquill.infrastructure.configuration import find_project
    from komaquill.infrastructure.sessions.connection import session_project

    settings = session_project(find_project(client.workspace)).settings()
    name = profile_name or settings.profiles[0]
    if name not in settings.presets or set(references) != set(settings.presets[name].image_inputs):
        raise KomaquillError(code="invalid_request", stage="preparation", retryable=False)
    return {
        key: client.inputs.import_file(
            base / path, kind=settings.presets[name].image_inputs[key].kind
        )
        for key, path in references.items()
    }


def render_file(client: Client, path: Path) -> tuple[RenderConfig, PromptSource]:
    """Import every scene's materials before calling the rendering service."""
    source = read_source(path, "input")
    try:
        value = json.loads(source.content.removeprefix("\ufeff"))
        for scene in value["scenes"]:
            scene["inputs"] = material_inputs(
                client, scene["profile"], scene.get("inputs", {}), path.parent
            )
        return RenderConfig.model_validate(value), source
    except (ValueError, KeyError, TypeError):
        raise KomaquillError(
            code="invalid_request", stage="story_rendering", retryable=False
        ) from None
