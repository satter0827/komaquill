"""Describe workspace paths without configuration or persistence dependencies."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspacePaths:
    """Keep application files beneath one explicitly selected workspace."""

    root: Path

    @property
    def artifacts(self) -> Path:
        return self.root / ".artifacts"

    @property
    def runtime(self) -> Path:
        return self.artifacts / "runtime"

    @property
    def state(self) -> Path:
        return self.artifacts / "state"

    @property
    def session_file(self) -> Path:
        return self.state / "session.json"

    @property
    def runs(self) -> Path:
        return self.artifacts / "runs"
