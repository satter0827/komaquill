"""Apply fixed retention rules without accessing processes or files."""

import re

CACHE_NAMES = frozenset(
    {
        "coverage",
        "uv",
        "mypy",
        "pycache",
        "ruff",
        "import-linter",
        "huggingface",
        "xet",
        "model-metadata",
    }
)
TEMP_PREFIXES = ("pytest-", "quality-audit-", "atomic-audit-", "build-python-")


def exclusion(
    category: str, name: str, protected: set[str], known_report: bool, busy: bool
) -> str | None:
    """Reject unknown, retained and actively used directories."""
    if category == "dist":
        if name in protected:
            return "retained"
        if not re.fullmatch(r"[0-9a-f]{32}", name) or not known_report:
            return "unknown"
    elif category == "cache":
        if name not in CACHE_NAMES:
            return "unknown"
        if busy and name != "uv":
            return "in_use"
    elif category == "tmp":
        if not name.startswith(TEMP_PREFIXES):
            return "unknown"
        if busy:
            return "in_use"
    else:
        return "protected"
    return None
