"""Verify platform contracts within the declared test scope."""

import os
import site
import subprocess
import sys
from textwrap import dedent

import pytest
from komaquill.adapters import provenance
from komaquill.resources import messages


def test_transferred_workflows_use_their_complete_reduced_catalog(tmp_path):
    """Prepare all packaged views without falling back to the local full catalog."""
    root = tmp_path / "worker"
    for name, content in provenance.gpu_contents():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    script = f"import sys; sys.path[:0]={[str(root), *site.getsitepackages()]!r}\n" + dedent(
        """\
        import tomllib
        from importlib.resources import files
        from komaquill.domain.configuration import RuntimeSettings
        from komaquill.adapters.preset_catalog import catalog
        from komaquill.adapters.workflows import prepare

        defaults = tomllib.loads(files('komaquill').joinpath(
            'resources/settings/defaults.toml').read_text())
        settings = RuntimeSettings.model_validate({**defaults, **catalog()})
        for name, preset in settings.presets.items():
            enabled = settings.model_copy(update={'models': (preset.model_id,)})
            graph, view = prepare(preset.sample_prompt, 1, name,
                settings=enabled, profile_name=name)
            assert graph and view['nodes']
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        cwd=root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_catalog_loader_rejects_missing_or_invalid_content(monkeypatch, tmp_path):
    monkeypatch.setattr(messages, "files", lambda _: tmp_path)
    messages.catalog.cache_clear()
    try:
        with pytest.raises(messages.MessageCatalogError):
            messages.catalog()
        (tmp_path / "resources").mkdir()
        (tmp_path / "resources/messages.json").write_text("[]")
        with pytest.raises(messages.MessageCatalogError):
            messages.catalog()
    finally:
        messages.catalog.cache_clear()
