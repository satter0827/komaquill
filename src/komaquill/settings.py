"""Define validated settings and workspace paths without import side effects."""

import os
import tomllib
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from komaquill.errors import KomaquillError


class Settings(BaseSettings):
    """Validate transport and operation limits."""

    model_config = SettingsConfigDict(
        env_prefix="KOMAQUILL_", extra="forbid", frozen=True, allow_inf_nan=False
    )
    poll_interval: float = Field(gt=0)
    request_timeout: float = Field(gt=0)
    generation_timeout: float = Field(gt=0)
    preparation_timeout: float = Field(gt=0)
    shutdown_timeout: float = Field(gt=0)
    execution_timeout: float = Field(gt=0)
    control_timeout: float = Field(gt=0)
    local_port: int = Field(ge=1024, le=65535)
    remote_port: int = Field(ge=1024, le=65535)
    gpu: str = Field(pattern=r"^(T4|L4|G4|H100|A100)$")

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
class Project:
    """Keep all application files in one workspace."""

    root: Path
    effective_settings: Settings | None = None

    def configured(self, settings: Settings | None = None) -> "Project":
        """Bind a validated snapshot without changing the source configuration."""
        return Project(self.root, settings or self.settings())

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

    def settings(self) -> Settings:
        """Read workspace overrides and environment variables."""
        if self.effective_settings is not None:
            return self.effective_settings
        with (self.root / "komaquill.toml").open("rb") as handle:
            defaults = tomllib.load(handle)
        # Environment overrides workspace TOML; secrets are excluded from settings.
        for name in list(defaults):
            if f"KOMAQUILL_{name.upper()}" in os.environ:
                del defaults[name]
        return Settings(**defaults)


def find_project(explicit: Path | None = None) -> Project:
    """Find the workspace configuration."""
    start = explicit or Path(os.environ.get("KOMAQUILL_PROJECT_DIR", Path.cwd()))
    start = start.resolve()
    candidates = [start] if explicit else [start, *start.parents]
    for candidate in candidates:
        if (candidate / "komaquill.toml").is_file():
            return Project(candidate)
    raise KomaquillError("作業フォルダーが見つかりません。--project で指定してください。")


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
