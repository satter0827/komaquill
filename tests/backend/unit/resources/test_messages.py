"""Verify platform contracts within the declared test scope."""

import json

import pytest
from komaquill.adapters import provenance
from komaquill.domain.errors import KomaquillError
from komaquill.resources import messages


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
        (name, content + b"\n# local edit" if name.endswith("cli/main.py") else content)
        for name, content in original
    ]
    monkeypatch.setattr(provenance, "package_contents", lambda: iter(changed))
    assert provenance.code_sha256(provenance.gpu_contents()) == baseline
    assert provenance.code_sha256(changed) != provenance.code_sha256(original)
    gpu = dict(provenance.gpu_contents())
    assert "komaquill/interfaces/cli/main.py" not in gpu
    assert "komaquill/adapters/colab.py" not in gpu
    text = json.loads(gpu["komaquill/resources/messages.json"])
    assert "text.describe_the_image_to_generate" not in text


def test_display_arguments_are_separate_from_request_context():
    error = KomaquillError(
        display_key="text.cannot_open_the_browser_open_this_url_url",
        details={"url": "http://127.0.0.1:18188", "run_id": "safe-request"},
    )
    text = messages.error_message(error)
    assert "http://127.0.0.1:18188" in text and "safe-request" in text
