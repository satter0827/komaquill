"""Provide explicit pinned assets for offline test boundaries."""

from komaquill.adapters.configuration import Settings
from komaquill.domain.presets import ModelPreset


def asset(name: str = "klein-4b") -> ModelPreset:
    return Settings().assets[name]
