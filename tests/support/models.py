"""Stage tiny, honestly hashed model fixtures for offline transport tests."""

import hashlib

from komaquill.infrastructure.asset_store import model_path, register
from komaquill.infrastructure.configuration import Project


def stage_models(project: Project) -> None:
    """Keep fake model contents explicit and prevent real downloads in tests."""
    payloads = {
        name: f"Offline model fixture: {name}".encode() for name in project.settings().assets
    }
    config = project.root / "komaquill.toml"
    text = config.read_text(encoding="utf-8")
    for name, content in payloads.items():
        text += (
            f'\n[assets.{name}]\nsha256 = "{hashlib.sha256(content).hexdigest()}"\n'
            f"size_bytes = {len(content)}\n"
        )
    config.write_text(text, encoding="utf-8")
    for name, model in project.settings().assets.items():
        path = model_path(project.models, model)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payloads[name])
        register(project.models, model, path)
