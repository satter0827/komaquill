"""Provide explicit pinned assets for offline test boundaries."""

from komaquill.domain.presets import ModelPreset
from komaquill.infrastructure.configuration import Settings


def asset(name: str = "klein-4b") -> ModelPreset:
    return Settings().assets[name]
