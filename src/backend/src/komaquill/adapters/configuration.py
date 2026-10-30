"""Define validated settings and workspace paths without import side effects."""

from __future__ import annotations

import tomllib
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from pydantic import Strict, TypeAdapter
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from komaquill.adapters.preset_catalog import catalog
from komaquill.domain.configuration import RuntimeSettings


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


class TypedEnvSettingsSource(EnvSettingsSource):
    """Decode strict primitive fields at the textual environment boundary."""

    def prepare_field_value(
        self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool
    ) -> Any:
        if (
            isinstance(value, str)
            and field.annotation in {bool, int}
            and any(isinstance(item, Strict) and item.strict for item in field.metadata)
        ):
            value = TypeAdapter(field.annotation).validate_python(value)
        return super().prepare_field_value(field_name, field, value, value_is_complex)


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
            TypedEnvSettingsSource(settings_cls),
            defaults,
            InitSettingsSource(settings_cls, init_kwargs=definitions),
        )


def load_runtime_settings(root: Path) -> RuntimeSettings:
    with (root / "komaquill.toml").open("rb") as handle:
        defaults = tomllib.load(handle)
    defaults.pop("story", None)
    defaults.pop("llm_profiles", None)
    defaults.pop("conversation", None)
    combined = merge_settings(catalog(root), defaults)
    combined = merge_settings(combined, TypedEnvSettingsSource(Settings)())
    return RuntimeSettings.model_validate(Settings.model_validate(combined).model_dump())
