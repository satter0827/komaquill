"""Locate a verbatim passage in the accepted user message."""

import re
from collections.abc import Sequence
from typing import Self

from pydantic import Field, model_validator

from komaquill.domain.roleplay import Record

_SENTENCE_END = re.compile(r"[。！？!?;；\n]|\.(?=\s|$)")
_TERMINATORS = "。！？!?;；.\n \t"
_SINGLE_QUOTE = re.compile(r"(?<!\w)['‘]|['’](?!\w)")
_PASSWORD_ASSIGNMENT = re.compile(
    r"(?:合言葉|パスワード)は\s*[「『“\"]?(?P<ja>[^」』”\"。\n]+?)(?:[」』”\"]|です|だ。|。|$)"
    r"|\b(?:password|passphrase|watchword) is\s+[\"“']?(?P<en>[^.\"”!?\n]+)",
    re.IGNORECASE,
)
_HYPOTHETICAL_OR_REPORTED = re.compile(
    r"\b(?:if|unless|could|might|would|maybe|suppose|suggest(?:ed|s)?|said|told|reported)\b"
    r"|\b(?:how about|not sure|don['’]t know|wonder|consider|discussed|wrote|texted)\b"
    r"|なら|たら|れば|場合|仮に|どうでしょう|どうですか|という案|という話|と言(?:った|って|う)"
    r"|と提案|と聞|かもしれ|か検討",
    re.IGNORECASE,
)
_SEPARATE_REQUEST = re.compile(
    r"\b(?:and|also|then|but)\s+(?:what|when|where|how|why|who|tell|explain|suggest|describe)\b"
    r"|(?:それと|それから|加えて|ついでに)",
    re.IGNORECASE,
)


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
        return " ".join(
            part
            for part in (
                text[: self.start].strip(),
                text[self.end :].lstrip("。．.!！、,;； \t\n").rstrip(),
            )
            if part
        )

    def overlaps(self, other: "SourceSpan") -> bool:
        return self.start < other.end and other.start < self.end

    def context(self, text: str) -> str:
        self.read(text)
        start = max((end.end() for end in _SENTENCE_END.finditer(text, 0, self.start)), default=0)
        last = len(text[: self.end].rstrip()) - 1
        end = _SENTENCE_END.search(text, last)
        return text[start : end.end() if end else len(text)]

    def is_single_sentence(self, text: str) -> bool:
        return _SENTENCE_END.search(self.read(text).rstrip(_TERMINATORS)) is None


def is_quoted(text: str, source: SourceSpan) -> bool:
    prefix = text[: source.start]
    return bool(
        source.read(text).lstrip().startswith(('"', "'", "「", "『", "“", "‘"))
        or prefix.count('"') % 2
        or len(_SINGLE_QUOTE.findall(prefix)) % 2
        or any(
            prefix.count(opening) > prefix.count(closing)
            for opening, closing in (("「", "」"), ("『", "』"), ("“", "”"))
        )
    )


def is_direct_statement(text: str, source: SourceSpan) -> bool:
    context = source.context(text)
    return not (
        is_quoted(text, source)
        or _HYPOTHETICAL_OR_REPORTED.search(context)
        or "?" in context
        or "？" in context
    )


def has_other_content(text: str, spans: Sequence[SourceSpan]) -> bool:
    return any(
        character.isalnum() and not any(span.start <= index < span.end for span in spans)
        for index, character in enumerate(text)
    )


def has_separate_request(text: str) -> bool:
    return _SEPARATE_REQUEST.search(text) is not None


def named_only_as_password(text: str) -> frozenset[str]:
    labels = set()
    for match in _PASSWORD_ASSIGNMENT.finditer(text):
        name = (match["ja"] or match["en"]).strip("'’ \t").casefold()
        other = text[: match.start()] + text[match.end() :]
        if name and name not in other.casefold():
            labels.add(name)
    return frozenset(labels)
