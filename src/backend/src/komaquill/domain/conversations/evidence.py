"""Locate a verbatim passage in the accepted user message."""

from typing import Self

from pydantic import Field, model_validator

from komaquill.domain.roleplay import Record


class SourceSpan(Record):
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if self.end <= self.start:
            raise ValueError("A source span must be nonempty.")
        return self

    @classmethod
    def locate(cls, text: str, quote: str) -> Self:
        start = text.find(quote)
        if not quote.strip() or start < 0 or text.find(quote, start + 1) >= 0:
            raise ValueError("The quotation must occur exactly once in the accepted input.")
        return cls(start=start, end=start + len(quote))

    def read(self, text: str) -> str:
        if self.end > len(text) or not text[self.start : self.end].strip():
            raise ValueError("The source span is outside the accepted input.")
        return text[self.start : self.end]

    def remaining(self, text: str) -> str:
        self.read(text)
        return (text[: self.start] + text[self.end :]).strip()
