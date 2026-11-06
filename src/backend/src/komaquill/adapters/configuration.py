"""Define validated settings and workspace paths without import side effects."""

from __future__ import annotations

import tomllib
from functools import cached_property
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from komaquill.adapters.llm_configuration import ConversationSettings
    from komaquill.adapters.web_configuration import WebSettings

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


class WorkspaceConfiguration:
    """Capture file and environment sources once, validating each feature independently."""

    def __init__(self, root: Path) -> None:
        from komaquill.adapters.llm_configuration import ConversationOptions, ProfileOptions
        from komaquill.adapters.web_configuration import WebOptions, defaults

        self.root = root
        self._web_env = EnvSettingsSource(WebOptions)
        self._web_defaults = defaults()
        with (root / "komaquill.toml").open("rb") as handle:
            self._configured = tomllib.load(handle)
        self._runtime_env = TypedEnvSettingsSource(Settings)
        self._conversation_env = EnvSettingsSource(ConversationOptions)
        self._profiles_env = EnvSettingsSource(ProfileOptions)
        self._runtime_defaults = tomllib.loads(
            files("komaquill").joinpath("resources/settings/defaults.toml").read_text("utf-8")
        )
        self._conversation_defaults = tomllib.loads(
            files("komaquill").joinpath("resources/conversations/defaults.toml").read_text("utf-8")
        )
        self._definitions = catalog(root)

    @cached_property
    def runtime(self) -> RuntimeSettings:
        snapshot = self

        class ResolvedRuntime(Settings):
            @classmethod
            def settings_customise_sources(
                cls,
                settings_cls: type[BaseSettings],
                init_settings: PydanticBaseSettingsSource,
                env_settings: PydanticBaseSettingsSource,
                dotenv_settings: PydanticBaseSettingsSource,
                file_secret_settings: PydanticBaseSettingsSource,
            ) -> tuple[PydanticBaseSettingsSource, ...]:
                values = {
                    key: value
                    for key, value in snapshot._configured.items()
                    if key not in {"story", "conversation", "llm_profiles", "web"}
                }
                return (
                    init_settings,
                    snapshot._runtime_env,
                    InitSettingsSource(settings_cls, init_kwargs=values),
                    InitSettingsSource(settings_cls, init_kwargs=snapshot._runtime_defaults),
                    InitSettingsSource(settings_cls, init_kwargs=snapshot._definitions),
                )

        values: dict[str, Any] = {}
        return RuntimeSettings.model_validate(ResolvedRuntime(**values).model_dump())

    @cached_property
    def web(self) -> WebSettings:
        from komaquill.adapters.web_configuration import WebSettings

        return WebSettings.model_validate(
            {**self._web_defaults, **self._configured.get("web", {}), **self._web_env()}
        )

    @cached_property
    def conversation(self) -> ConversationSettings:
        from komaquill.adapters.llm_configuration import ConversationOptions, ConversationSettings

        snapshot = self

        class ResolvedConversation(ConversationOptions):
            @classmethod
            def settings_customise_sources(
                cls,
                settings_cls: type[BaseSettings],
                init_settings: PydanticBaseSettingsSource,
                env_settings: PydanticBaseSettingsSource,
                dotenv_settings: PydanticBaseSettingsSource,
                file_secret_settings: PydanticBaseSettingsSource,
            ) -> tuple[PydanticBaseSettingsSource, ...]:
                profiles = snapshot._configured.get("llm_profiles", {})
                return (
                    init_settings,
                    snapshot._profiles_env,
                    snapshot._conversation_env,
                    InitSettingsSource(settings_cls, init_kwargs={"llm_profiles": profiles}),
                    InitSettingsSource(
                        settings_cls, init_kwargs=snapshot._configured.get("conversation", {})
                    ),
                    InitSettingsSource(settings_cls, init_kwargs=snapshot._conversation_defaults),
                )

        values: dict[str, Any] = {}
        return ConversationSettings.model_validate(ResolvedConversation(**values).model_dump())
