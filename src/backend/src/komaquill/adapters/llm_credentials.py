"""Resolve a credential only for an explicitly selected connection."""

import os
from urllib.parse import urlsplit

import keyring

from komaquill.adapters.llm_configuration import LLMProfile
from komaquill.domain.errors import KomaquillError


def api_key(profile: LLMProfile) -> str:
    key = os.environ.get(profile.key_env) if profile.key_env else None
    if key is None and profile.credential_ref:
        key = keyring.get_password("komaquill.llm", profile.credential_ref)
    if key is None and urlsplit(profile.base_url or "").hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        key = "local"
    if not key:
        raise KomaquillError(code="authentication_failed", retryable=False)
    return key
