"""Define validated settings and workspace paths without import side effects."""

import os
import tomllib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from komaquill.domain.models import Session
    from komaquill.domain.presets import ModelPreset
    from komaquill.infrastructure.artifacts import LocalArtifactStore
    from komaquill.infrastructure.storage import FileRunRepository
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.paths import GpuPaths, WorkspacePaths
from komaquill.infrastructure.preset_catalog import catalog


def merge_settings(base: dict[str, object], changes: dict[str, object]) -> dict[str, object]:
    """Apply leaf overrides without discarding unspecified profile values."""
    result = dict(base)
    for key, value in changes.items():
        prior = result.get(key)
        result[key] = (
            merge_settings(prior, value)
            if isinstance(prior, dict) and isinstance(value, dict)
            else value
        )
    return result


class Settings(RuntimeSettings, BaseSettings):
    """Validate transport and operation limits."""

    model_config = SettingsConfigDict(
        env_prefix="KOMAQUILL_",
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        env_nested_delimiter="__",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Use typed packaged defaults without loading dotenv or secret files."""
        with as_file(files("komaquill").joinpath("resources/settings/defaults.toml")) as path:
            defaults = TomlConfigSettingsSource(settings_cls, toml_file=path)
        definitions = catalog()
        return (
            init_settings,
            env_settings,
            defaults,
            InitSettingsSource(settings_cls, init_kwargs=definitions),
        )


@dataclass(frozen=True)
class Project(WorkspacePaths):
    """Keep all application files in one workspace."""

    effective_settings: RuntimeSettings | None = None

    def configured(self, settings: RuntimeSettings | None = None) -> "Project":
        """Bind a validated snapshot without changing the source configuration."""
        return type(self)(self.root, settings or self.settings())

    def settings(self) -> RuntimeSettings:
        """Read workspace overrides and environment variables."""
        if self.effective_settings is not None:
            return self.effective_settings
        with (self.root / "komaquill.toml").open("rb") as handle:
            defaults = tomllib.load(handle)
        defaults.pop("story", None)
        defaults.pop("llm_profiles", None)
        combined = merge_settings(catalog(self.root), defaults)
        combined = merge_settings(combined, EnvSettingsSource(Settings)())
        return RuntimeSettings.model_validate(Settings.model_validate(combined).model_dump())

    @property
    def repository(self) -> "FileRunRepository":
        """Bind persistence to this workspace without reading configuration."""
        from komaquill.infrastructure.storage import FileRunRepository

        return FileRunRepository(self)

    @property
    def artifacts_store(self) -> "LocalArtifactStore":
        from komaquill.infrastructure.artifacts import LocalArtifactStore

        return LocalArtifactStore(self)

    def accepts_results(self, session: "Session") -> bool:
        """Reject results belonging to a replaced or finalized session."""
        from komaquill.infrastructure.storage import load_session

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

        from komaquill.infrastructure.provenance import code_sha256, source_revision

        return {
            "app_version": version("komaquill"),
            "source_revision": source_revision(),
            "code_sha256": code_sha256(),
        }

    def prepare(
        self,
        prompt: str,
        negative: str | None,
        seed: int,
        run_id: str,
        profile_name: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from komaquill.infrastructure.workflows import prepare

        return prepare(
            prompt, negative, seed, run_id, settings=self.settings(), profile_name=profile_name
        )

    def stop_requested(self, session: "Session") -> bool | None:
        from komaquill.infrastructure.storage import stop_request

        return stop_request(self, session)


def find_project(explicit: Path | None = None) -> Project:
    """Find the workspace configuration."""
    start = explicit or Path(os.environ.get("KOMAQUILL_PROJECT_DIR", Path.cwd()))
    start = start.resolve()
    candidates = [start] if explicit else [start, *start.parents]
    for candidate in candidates:
        if (candidate / "komaquill.toml").is_file():
            project = Project(candidate)
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


def initialize(directory: Path) -> Project:
    """Prepare a workspace without overwriting existing configuration or ignore rules."""
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
    project = Project(root)
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


class GpuProject(Project):
    """Use transient server paths independently of the local data layout."""

    @property
    def data(self) -> Path:
        return GpuPaths(self.root).data

    @property
    def records(self) -> Path:
        return GpuPaths(self.root).records
