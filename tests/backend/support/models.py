"""Stage tiny, honestly hashed model fixtures for offline transport tests."""

import hashlib

from komaquill.adapters.asset_store import model_path, register
from komaquill.adapters.workflows import assets
from komaquill.adapters.workspace import RuntimeWorkspace


def stage_models(project: RuntimeWorkspace) -> None:
    """Keep fake model contents explicit and prevent real downloads in tests."""
    payloads = {
        model.name: f"Offline model fixture: {model.name}".encode()
        for model in assets(project.settings())
    }
    config = project.root / "komaquill.toml"
    text = config.read_text(encoding="utf-8")
    for name, content in payloads.items():
        text += (
            f'\n[assets.{name}]\nsha256 = "{hashlib.sha256(content).hexdigest()}"\n'
            f"size_bytes = {len(content)}\n"
        )
    config.write_text(text, encoding="utf-8")
    for model in assets(project.settings()):
        name = model.name
        path = model_path(project.models, model)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payloads[name])
        register(project.models, model, path)
