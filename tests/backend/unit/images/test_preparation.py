"""Verify images contracts within the declared test scope."""

from types import SimpleNamespace

import pytest
from komaquill.adapters import preparation as module
from komaquill.domain.assets import PROTOCOL_VERSION
from komaquill.domain.errors import KomaquillError


@pytest.mark.parametrize("case", ["supported", "bare", "failed"])
def test_capability_detection_does_not_change_standard_external_access(case):
    def query(path):
        if case != "supported":
            raise KomaquillError(code="http_failed", http_status=404 if case == "bare" else 500)
        return {"protocol_version": PROTOCOL_VERSION}

    if case == "failed":
        with pytest.raises(KomaquillError):
            module.capabilities(SimpleNamespace(json=query))
    else:
        assert module.capabilities(SimpleNamespace(json=query)) is (case == "supported")


@pytest.mark.parametrize("available", [True, False])
def test_managed_start_requires_a_functioning_extension(monkeypatch, available):
    monkeypatch.setattr(module, "capabilities", lambda _: available)
    if available:
        module.verify_extension(None)
    else:
        with pytest.raises(KomaquillError) as error:
            module.verify_extension(None)
        assert error.value.code == "extension_unavailable" and not error.value.retryable
