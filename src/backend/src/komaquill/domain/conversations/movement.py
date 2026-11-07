"""Ground immediate travel in the accepted message."""

import re

from komaquill.domain.conversations.actions import InferredMove, MoveAction
from komaquill.domain.conversations.evidence import (
    SourceSpan,
    has_separate_request,
    is_direct_statement,
)
from komaquill.domain.conversations.meetings import MEETING_TOPIC
from komaquill.domain.roleplay import MAX_PLACE_LENGTH

_DIRECT_TRAVEL = re.compile(
    r"(?:^|(?<=[。！？!?;；\n])|(?<=\. ))\s*(?:(?:一緒に|二人で|今すぐ|今から)\s*)*"
    r"(?P<ja>[^。！？!?、,\n]+?)(?:へ|に)"
    r"(?:行こう|行きましょう|向かおう|向かいましょう|移ろう|移りましょう|移り)(?=$|[。！、,\n])"
    r"|(?:^|(?<=[。！？!?;；\n])|(?<=\. ))\s*(?:(?:let['’]s|let us)\s+)?"
    r"(?:go|head|walk|move|return)\s+to\s+(?:the\s+)?(?P<en>[^.!?,;\n]+?)"
    r"(?:\s+(?:together|(?:right )?now))*(?=$|[.!,;\n])",
    re.IGNORECASE,
)
_TRAVEL_VERB = re.compile(
    r"\b(?:go|head|walk|move|return|travel)(?:ing|ed)?\b|行[こくき]|向か|移[りるろ動]|戻",
    re.IGNORECASE,
)
_FUTURE_OR_NEGATED = re.compile(
    r"\b(?:tomorrow|later|next|not|never|don['’]t|won['’]t|want to|plan to)\b"
    r"|明日|来週|あとで|後で|今度|行かない|向かわない|予定|つもり|行きたい",
    re.IGNORECASE,
)
_JOINED_REQUEST = re.compile(r"\b(?:and|then)\b", re.IGNORECASE)
_UNSPECIFIED_DESTINATION = re.compile(
    r"(?:there|here|over there|somewhere|anywhere|そこ|ここ|あそこ|そっち|あっち|どこか)",
    re.IGNORECASE,
)


def validate_move(move: InferredMove, text: str, place: str) -> None:
    clause = move.source.read(text)
    destination = move.action.destination.strip().casefold()
    grounded = (
        re.search(rf"(?<!\w){re.escape(destination)}(?!\w)", clause.casefold())
        if destination.isascii()
        else destination in clause.casefold()
    )
    context = move.source.context(text)
    if (
        not grounded
        or _UNSPECIFIED_DESTINATION.fullmatch(destination)
        or destination == place.strip().casefold()
        or not _TRAVEL_VERB.search(clause)
        or not move.source.is_single_sentence(text)
        or has_separate_request(clause)
        or not is_direct_statement(text, move.source)
        or MEETING_TOPIC.search(context)
        or _FUTURE_OR_NEGATED.search(context)
    ):
        raise ValueError("Immediate travel requires a stated destination and a direct move.")


def direct_move(text: str, place: str) -> InferredMove | None:
    for match in _DIRECT_TRAVEL.finditer(text):
        destination = (match["ja"] or match["en"]).strip()
        if not destination or len(destination) > MAX_PLACE_LENGTH:
            continue
        if match["en"] and _JOINED_REQUEST.search(destination):
            continue
        try:
            source = SourceSpan.locate(text, match[0].lstrip())
            move = InferredMove(action=MoveAction(destination=destination), source=source)
            validate_move(move, text, place)
        except ValueError:
            continue
        return move
    return None
