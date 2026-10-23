"""Define validated settings and workspace paths without import side effects."""

import os
import tomllib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from komaquill.domain.models import Session
    from komaquill.infrastructure.storage import FileRunRepository
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.paths import WorkspacePaths


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
        with as_file(files("komaquill").joinpath("resources/defaults.toml")) as path:
            defaults = TomlConfigSettingsSource(settings_cls, toml_file=path)
        return init_settings, env_settings, defaults


@dataclass(frozen=True)
class Project(WorkspacePaths):
    """Keep all application files in one workspace."""

    effective_settings: RuntimeSettings | None = None

    def configured(self, settings: RuntimeSettings | None = None) -> "Project":
        """Bind a validated snapshot without changing the source configuration."""
        return Project(self.root, settings or self.settings())

    def settings(self) -> RuntimeSettings:
        """Read workspace overrides and environment variables."""
        if self.effective_settings is not None:
            return self.effective_settings
        with (self.root / "komaquill.toml").open("rb") as handle:
            defaults = tomllib.load(handle)
        # Environment overrides workspace TOML; secrets are excluded from settings.
        for name in list(defaults):
            if f"KOMAQUILL_{name.upper()}" in os.environ:
                del defaults[name]
        return RuntimeSettings.model_validate(Settings(**defaults).model_dump())

    @property
    def repository(self) -> "FileRunRepository":
        """Bind persistence to this workspace without reading configuration."""
        from komaquill.infrastructure.storage import FileRunRepository

        return FileRunRepository(self)

    def identity(self) -> dict[str, Any]:
        from importlib.metadata import version

        from komaquill.infrastructure.provenance import code_sha256, source_revision

        return {
            "app_version": version("komaquill"),
            "source_revision": source_revision(),
            "code_sha256": code_sha256(),
        }

    def prepare(
        self, prompt: str, negative: str, seed: int, run_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from komaquill.infrastructure.workflows import prepare

        return prepare(prompt, negative, seed, run_id)

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
            return Project(candidate)
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
    project.settings()
    return project
