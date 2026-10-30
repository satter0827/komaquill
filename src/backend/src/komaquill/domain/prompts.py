"""Compose visible directions while omitting empty prompt components."""


def joined(*parts: str) -> str:
    """Join nonempty directions without introducing dangling separators."""
    return ", ".join(part.strip() for part in parts if part.strip())
