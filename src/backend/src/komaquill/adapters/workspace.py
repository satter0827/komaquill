"""Bind image and runtime ports to a workspace and an immutable settings snapshot."""

import os
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

from komaquill.adapters.paths import WorkspacePaths
from komaquill.domain.configuration import RecordedSettings
from komaquill.domain.errors import KomaquillError

if TYPE_CHECKING:
    from komaquill.adapters.artifacts import LocalArtifactStore
    from komaquill.adapters.storage import ImageRepository
    from komaquill.domain.images import CompiledImageInput
    from komaquill.domain.models import Session
    from komaquill.domain.presets import ModelPreset


@dataclass(frozen=True)
class RuntimeWorkspace(WorkspacePaths):
    """Keep all application files in one workspace."""

    effective_settings: RecordedSettings | None = None

    def configured(self, settings: RecordedSettings | None = None) -> "RuntimeWorkspace":
        """Bind a validated snapshot without changing the source configuration."""
        return type(self)(self.root, settings or self.settings())

    def settings(self) -> RecordedSettings:
        """Read workspace overrides and environment variables."""
        if self.effective_settings is not None:
            return self.effective_settings
        from komaquill.adapters.configuration import load_runtime_settings

        return load_runtime_settings(self.root)

    @property
    def repository(self) -> "ImageRepository":
        """Bind persistence to this workspace without reading configuration."""
        from komaquill.adapters.storage import ImageRepository

        return ImageRepository(self)

    @property
    def artifacts_store(self) -> "LocalArtifactStore":
        from komaquill.adapters.artifacts import LocalArtifactStore

        return LocalArtifactStore(self)

    def accepts_results(self, session: "Session") -> bool:
        """Reject results belonging to a replaced or finalized session."""
        from komaquill.adapters.storage import load_session

        if self.session_file.is_file() and load_session(self).runtime_id != session.runtime_id:
            return False
        import json

        outcome = self.logs / str(session.runtime_id) / "shutdown.json"
        if session.backend == "colab" and outcome.is_file():
            result = json.loads(outcome.read_bytes())
            return not (result.get("finished_at") and result.get("gpu") == "stopped")
        return True

    def prepared_assets(self, session: "Session") -> dict[str, "ModelPreset"]:
        """Read only bindings verified on the current remote runtime."""
        import json

        from komaquill.domain.presets import ModelPreset

        path = self.state / "model-bindings.json"
        if not path.is_file():
            return {}
        data = json.loads(path.read_bytes())
        if data.get("runtime_id") != str(session.runtime_id):
            return {}
        return {name: ModelPreset.model_validate(asset) for name, asset in data["assets"].items()}

    def identity(self) -> dict[str, Any]:
        from importlib.metadata import version

        from komaquill.adapters.provenance import code_sha256, source_revision

        return {
            "app_version": version("komaquill"),
            "source_revision": source_revision(),
            "code_sha256": code_sha256(),
        }

    def prepare(
        self,
        prompt: str,
        seed: int,
        run_id: str,
        profile_name: str | None = None,
        *,
        compiled_input: "CompiledImageInput | None" = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from komaquill.adapters.workflows import prepare

        return prepare(
            prompt,
            seed,
            run_id,
            settings=self.settings(),
            profile_name=profile_name,
            compiled_input=compiled_input,
        )

    def stop_requested(self, session: "Session") -> bool | None:
        from komaquill.adapters.storage import stop_request

        return stop_request(self, session)


def find_project(explicit: Path | None = None) -> RuntimeWorkspace:
    """Find the workspace configuration."""
    from komaquill.adapters.workspace import RuntimeWorkspace

    start = explicit or Path(os.environ.get("KOMAQUILL_PROJECT_DIR", Path.cwd()))
    start = start.resolve()
    candidates = [start] if explicit else [start, *start.parents]
    for candidate in candidates:
        if (candidate / "komaquill.toml").is_file():
            project = RuntimeWorkspace(candidate)
            migration = project.state / "storage-migration.json"
            if migration.is_file():
                import json

                if json.loads(migration.read_bytes()).get("status") != "complete":
                    raise KomaquillError(
                        display_key="cleanup.migration", code="invalid_configuration"
                    )
            if any(
                (project.artifacts / name).exists()
                for name in ("runs", "models", "inputs", "workflows", "logs", "reports")
            ):
                raise KomaquillError(display_key="cleanup.migration", code="invalid_configuration")
            return project
    raise KomaquillError(display_key="text.workspace_not_found_specify_project")


def initialize(directory: Path) -> RuntimeWorkspace:
    """Prepare a workspace without overwriting existing configuration or ignore rules."""
    from komaquill.adapters.workspace import RuntimeWorkspace

    root = directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = root / "komaquill.toml"
    if not config.exists():
        with config.open("x", encoding="utf-8") as handle:
            handle.write(
                "# Set project overrides here. Unspecified values use the packaged defaults.\n"
            )
    ignore = root / ".gitignore"
    if not ignore.exists():
        with ignore.open("x", encoding="utf-8") as handle:
            handle.write("/.artifacts/\n")
    else:
        rules = ignore.read_text(encoding="utf-8")
        if not any(row.strip() in {"/.artifacts/", ".artifacts/"} for row in rules.splitlines()):
            with ignore.open("a", encoding="utf-8") as handle:
                handle.write(("" if rules.endswith("\n") else "\n") + "/.artifacts/\n")
    project = RuntimeWorkspace(root)
    project.artifacts.mkdir(exist_ok=True)
    for folder in (project.data, project.records, project.state):
        folder.mkdir(exist_ok=True)
    guide = project.artifacts / "README.md"
    if not guide.exists():
        guide.write_text(
            files("komaquill").joinpath("resources/artifacts.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    project.settings()
    return project
