"""Validate exact HTTPS origins before exporting executable proxy configuration."""

import pytest
from komaquill.adapters.web_configuration import WebSettings, defaults
from pydantic import ValidationError


@pytest.mark.parametrize(
    "origin",
    [
        "http://home",
        "https://home/path",
        "https://home?",
        "https://home#",
        "https://@home",
        "https://user:secret@home",  # pragma: allowlist secret Invalid test-only endpoint.
        "https://home\\other",
        "https://home\nadmin",
    ],
)
def test_invalid_or_ambiguous_origins_are_rejected(origin):
    with pytest.raises(ValidationError):
        WebSettings.model_validate({**defaults(), "origin": origin})


def test_origins_use_browser_canonicalization():
    assert (
        WebSettings.model_validate({**defaults(), "origin": "https://HOME:443/"}).origin
        == "https://home"
    )
