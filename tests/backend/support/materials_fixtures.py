"""Share deterministic fixtures used by multiple test scopes."""

import hashlib
from uuid import uuid4

from komaquill.domain.materials import Material


def material(content=b"image", **changes):
    values = {
        "workspace_id": uuid4(),
        "kind": "inputs",
        "name": "source.png",
        "reference": "source.png",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    return Material.model_validate({**values, **changes})
