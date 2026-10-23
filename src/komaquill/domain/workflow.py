"""Hash generation conditions independently of serialization details."""

import hashlib
import json

from komaquill.domain.models import JsonObject


def digest(data: JsonObject) -> str:
    """Hash stable JSON without reading files or configuration."""
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
