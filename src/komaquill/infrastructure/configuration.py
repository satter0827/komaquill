"""Define validated settings and workspace paths without import side effects."""

import os
import tomllib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from komaquill.domain.models import Session
    from komaquill.domain.presets import ModelPreset
    from komaquill.infrastructure.storage import FileRunRepository
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.paths import WorkspacePaths
from komaquill.infrastructure.preset_catalog import catalog


class Settings(RuntimeSettings, BaseSettings):
    """Validate transport and operation limits."""

    model_config = SettingsConfigDict(
        env_prefix="KOMAQUILL_", extra="forbid", frozen=True, allow_inf_nan=False
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
        definitions = catalog(self.root)
        for section in ("assets", "presets"):
            for name, changes in defaults.pop(section, {}).items():
                definitions[section][name] = {**definitions[section].get(name, {}), **changes}
        import json

        for name in list(defaults):
            if f"KOMAQUILL_{name.upper()}" in os.environ:
                del defaults[name]
        for section in ("assets", "presets"):
            raw = os.environ.get(f"KOMAQUILL_{section.upper()}")
            if raw is not None:
                for name, changes in json.loads(raw).items():
                    definitions[section][name] = {**definitions[section].get(name, {}), **changes}
        return RuntimeSettings.model_validate(Settings(**definitions, **defaults).model_dump())

    @property
    def repository(self) -> "FileRunRepository":
        """Bind persistence to this workspace without reading configuration."""
        from komaquill.infrastructure.storage import FileRunRepository

        return FileRunRepository(self)

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
        return self.artifacts

    @property
    def records(self) -> Path:
        return self.artifacts
