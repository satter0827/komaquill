"""Hash version-four immutable history without depending on model or transport types."""

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from komaquill.domain.conversations.models import Turn


def memory_sources(turn: "Turn") -> dict[str, str]:
    values = {
        field: getattr(turn, field)
        for field in ("user", "assistant", "narration", "action_narration")
        if getattr(turn, field)
    }
    if turn.action is not None:
        values["action"] = turn.action.model_dump_json()
    return values


def advance_history(previous: str, turn: dict[str, Any]) -> str:
    value = {
        key: turn.get(key, default)
        for key, default in (
            ("user", ""),
            ("assistant", ""),
            ("narration", ""),
            ("action_narration", ""),
            ("action_id", None),
            ("action", None),
            ("notes", []),
            ("state", {}),
        )
    }
    for field in ("meeting", "meeting_state"):
        if turn.get(field) is not None:
            value[field] = turn[field]
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((previous + "\n" + encoded).encode()).hexdigest()
