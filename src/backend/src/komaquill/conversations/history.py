"""Hash version-four immutable history without depending on model or transport types."""

import hashlib
import json
from typing import Any


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
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((previous + "\n" + encoded).encode()).hexdigest()
