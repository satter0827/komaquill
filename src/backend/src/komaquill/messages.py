"""Load English presentation text without importing application dependencies."""

import json
from functools import lru_cache
from importlib.resources import files
from string import Formatter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from komaquill.domain.errors import KomaquillError


class MessageCatalogError(RuntimeError):
    """Report an unusable catalog without printing its contents."""


@lru_cache(maxsize=1)
def catalog() -> dict[str, str]:
    """Load packaged text once per process."""
    try:
        data = json.loads(
            files("komaquill").joinpath("resources/messages.json").read_text(encoding="utf-8")
        )
        if not isinstance(data, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in data.items()
        ):
            raise ValueError
        return data
    except (OSError, ValueError) as error:
        raise MessageCatalogError(
            "Message definitions are unavailable. Reinstall Komaquill."
        ) from error


def message(key: str, **values: object) -> str:
    """Format one complete message with exactly its declared named values."""
    try:
        text = catalog()[key]
        expected = {name for _, name, _, _ in Formatter().parse(text) if name is not None}
        if expected != values.keys():
            raise ValueError
        return text.format(**values)
    except (KeyError, ValueError, IndexError) as error:
        raise MessageCatalogError(
            "Message definitions are invalid. Reinstall Komaquill."
        ) from error


def error_message(error: "KomaquillError") -> str:
    """Render domain codes without copying arbitrary exception text."""
    key = error.display_key or "error." + error.code.value
    expected = {name for _, name, _, _ in Formatter().parse(catalog()[key]) if name is not None}
    explanation = message(
        key, **{name: value for name, value in error.details.items() if name in expected}
    )
    if error.details.get("run_id"):
        return "\n".join(
            (
                explanation,
                message("error.run_retry", code=error.code.value, run_id=error.details["run_id"]),
            )
        )
    return explanation
