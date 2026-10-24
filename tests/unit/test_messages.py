"""Check catalog references, formatting and deployment boundaries."""

import ast
import json
from pathlib import Path
from string import Formatter

import pytest

from komaquill import messages
from komaquill.domain.errors import ErrorCode, KomaquillError
from komaquill.infrastructure import provenance

ROOT = Path(__file__).resolve().parents[2]


def test_catalog_keys_are_unique_and_error_categories_are_explained():
    def unique(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result
            result[key] = value
        return result

    data = json.loads(
        (ROOT / "src/komaquill/resources/messages.json").read_text(encoding="utf-8"),
        object_pairs_hook=unique,
    )
    assert all("error." + code.value in data for code in ErrorCode)
    assert all(isinstance(value, str) and value.strip() for value in data.values())


def test_literal_message_references_and_named_values_match_the_catalog():
    data = messages.catalog()
    for path in [*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "message" and node.args:
                key = node.args[0]
                if isinstance(key, ast.Constant):
                    assert key.value in data, (path, key.value)
                    required = {
                        n for _, n, _, _ in Formatter().parse(data[key.value]) if n is not None
                    }
                    assert required == {k.arg for k in node.keywords}, (path, key.value)
            for kw in node.keywords:
                if kw.arg == "display_key" and isinstance(kw.value, ast.Constant):
                    assert kw.value.value in data, (path, kw.value.value)


def test_invalid_message_keys_or_values_fail_safely(monkeypatch):
    with pytest.raises(messages.MessageCatalogError):
        messages.message("missing")
    with pytest.raises(messages.MessageCatalogError):
        messages.message("status.pending", wrong=1)
    assert messages.message("status.pending", count=2).endswith("2")
    error = KomaquillError("private-response", code="internal_error")
    assert "private-response" not in messages.error_message(error)
    assert "collect" in messages.error_message(
        KomaquillError(code="collection_failed", details={"run_id": "safe-run"})
    )
    monkeypatch.setattr(messages, "catalog", lambda: {"broken": "{item[missing]}"})
    with pytest.raises(messages.MessageCatalogError):
        messages.message("broken", item={})


def test_gpu_fingerprint_ignores_cli_only_code_and_text(monkeypatch):
    original = list(provenance.package_contents())
    baseline = provenance.code_sha256(provenance.gpu_contents())
    changed = [
        (name, content + b"\n# local edit" if name.endswith("interfaces/cli.py") else content)
        for name, content in original
    ]
    monkeypatch.setattr(provenance, "package_contents", lambda: iter(changed))
    assert provenance.code_sha256(provenance.gpu_contents()) == baseline
    assert provenance.code_sha256(changed) != provenance.code_sha256(original)
    gpu = dict(provenance.gpu_contents())
    assert "komaquill/interfaces/cli.py" not in gpu
    assert "komaquill/infrastructure/colab.py" not in gpu
    text = json.loads(gpu["komaquill/resources/messages.json"])
    assert "text.describe_the_image_to_generate" not in text


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


def test_extension_catalog_and_executable_resources_are_packaged():
    import re

    package = ROOT / "src/komaquill"
    scripts = list((package / "interfaces/comfyui/web").glob("*.js"))
    assert scripts
    for path in scripts:
        name = path.relative_to(package).as_posix()
        source = path.read_text(encoding="utf-8")
        for key in re.findall(r'"(ui\.[a-z_]+)"', source):
            assert key in messages.catalog()
        assert "komaquill/" + name in dict(provenance.gpu_contents())
