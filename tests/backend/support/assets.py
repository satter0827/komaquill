"""Build deterministic synthetic assets with hashes paired to their bytes."""

import hashlib

from komaquill.domain.presets import ModelPreset


def fake_asset(content=b"model", **changes):
    return ModelPreset(
        name="model",
        filename="model.safetensors",
        sha256=hashlib.sha256(content).hexdigest(),
        url="https://huggingface.co/owner/repo/resolve/" + "a" * 40 + "/model.safetensors",
        license="test",
        **changes,
    )
