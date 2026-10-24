"""Describe local storage categories without reading configuration or creating files."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspacePaths:
    """Separate durable data and evidence from recreatable working files."""

    root: Path

    @property
    def artifacts(self) -> Path:
        return self.root / ".artifacts"

    @property
    def data(self) -> Path:
        return self.artifacts / "data"

    @property
    def records(self) -> Path:
        return self.artifacts / "records"

    @property
    def models(self) -> Path:
        return self.data / "models"

    @property
    def runs(self) -> Path:
        return self.data / "runs"

    @property
    def inputs(self) -> Path:
        return self.data / "inputs"

    @property
    def workflows(self) -> Path:
        return self.data / "workflows"

    @property
    def logs(self) -> Path:
        return self.records / "logs"

    @property
    def reports(self) -> Path:
        return self.records / "reports"

    @property
    def runtime(self) -> Path:
        return self.artifacts / "runtime"

    @property
    def cache(self) -> Path:
        return self.artifacts / "cache"

    @property
    def temporary(self) -> Path:
        return self.artifacts / "tmp"

    @property
    def distributions(self) -> Path:
        return self.artifacts / "dist"

    @property
    def state(self) -> Path:
        return self.artifacts / "state"

    @property
    def session_file(self) -> Path:
        return self.state / "session.json"


class GpuPaths(WorkspacePaths):
    """Keep transient GPU files independent of local preservation categories."""

    @property
    def data(self) -> Path:
        return self.artifacts

    @property
    def records(self) -> Path:
        return self.artifacts
