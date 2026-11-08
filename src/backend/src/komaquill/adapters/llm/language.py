"""Validate visible response language without depending on model execution."""

import re

JAPANESE_TEXT = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
MIN_LANGUAGE_CHARACTERS = 12


def check_language(text: str, locale: str, names: tuple[str, ...] = ()) -> None:
    for name in sorted(names, key=len, reverse=True):
        text = text.replace(name, "")
    japanese = bool(JAPANESE_TEXT.search(text))
    if (locale == "en" and japanese) or (
        locale == "ja" and len(text.strip()) >= MIN_LANGUAGE_CHARACTERS and not japanese
    ):
        raise ResponseLanguageError("output")


class ResponseLanguageError(ValueError):
    def __init__(self, *fields: str) -> None:
        self.fields = fields
        super().__init__()
