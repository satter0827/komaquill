"""Read portable prompt files at the terminal boundary."""

from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import ImageInput, PromptSource


def read_source(path: Path, role: Literal["positive", "negative", "input"]) -> PromptSource:
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


def negative_input(
    negative: str | None, negative_file: Path | None
) -> tuple[str | None, tuple[PromptSource, ...]]:
    if negative is not None and negative_file is not None:
        raise KomaquillError(
            code="invalid_request",
            stage="generation",
            display_key="inputs.negative_conflict",
            retryable=False,
        )
    if negative_file is None:
        return negative, ()
    source = read_source(negative_file, "negative")
    return source.content.removeprefix("\ufeff"), (source,)


def image_input(
    prompt: str | None,
    negative: str | None,
    prompt_file: Path | None = None,
    negative_file: Path | None = None,
    input_file: Path | None = None,
) -> tuple[ImageInput, tuple[PromptSource, ...]]:
    if input_file is not None:
        if any(value is not None for value in (prompt, negative, prompt_file, negative_file)):
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
    negative, extra = negative_input(negative, negative_file)
    try:
        value = ImageInput.model_validate({"prompt": prompt, "negative": negative})
    except ValidationError:
        raise KomaquillError(
            code="invalid_request",
            stage="generation",
            display_key="inputs.invalid_prompt",
            retryable=False,
        ) from None
    return value, (*sources, *extra)
