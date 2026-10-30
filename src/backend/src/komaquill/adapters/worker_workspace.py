"""Bind frozen worker settings without local storage or database dependencies."""

from dataclasses import dataclass

from komaquill.adapters.paths import GpuPaths
from komaquill.domain.configuration import RecordedSettings


@dataclass(frozen=True)
class WorkerWorkspace(GpuPaths):
    effective_settings: RecordedSettings

    def settings(self) -> RecordedSettings:
        return self.effective_settings
