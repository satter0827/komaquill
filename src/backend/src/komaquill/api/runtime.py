"""Compose image, session and evaluation use cases for Python and terminal callers."""

from collections.abc import Callable
from pathlib import Path
from secrets import randbelow
from typing import Literal

from komaquill.adapters.compute import inspection, shutdown, supervisor
from komaquill.adapters.diagnostics import operation_scope
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.api._errors import api_operation
from komaquill.domain.configuration import GpuType, RecordedSettings, RuntimeSettings
from komaquill.domain.errors import ErrorCode, KomaquillError, OperationStage
from komaquill.domain.images import MAX_BROWSER_SEED
from komaquill.domain.library.models import ImagePromptDefaults, WaiPreferences
from komaquill.domain.models import JsonObject
from komaquill.domain.presets import WAI_PROFILE
from komaquill.domain.status import SessionStatus
from komaquill.domain.usage import AccountUsage


class RuntimeService:
    def __init__(
        self,
        project: RuntimeWorkspace,
        image_preferences: Callable[[], ImagePromptDefaults] = ImagePromptDefaults,
    ) -> None:
        self._project = project
        self._image_preferences = image_preferences

    def wai_defaults(self) -> tuple[WaiPreferences, int, dict[str, tuple[str, ...]]]:
        from komaquill.adapters.compute.connection import session_project

        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            settings = session_project(self._project).settings()
            if not isinstance(settings, RuntimeSettings):
                raise KomaquillError(code="invalid_configuration", stage="check")
            preset = settings.presets[WAI_PROFILE]
            defaults = WaiPreferences(
                quality_enabled=preset.quality_enabled,
                quality_positive=preset.positive_prefix,
                quality_negative=preset.negative_default,
                candidates=preset.batch_size,
                width=preset.width,
                height=preset.height,
                steps=preset.steps,
                cfg=float(preset.parameters["cfg"]),
                sampler=preset.sampler,
                scheduler=preset.scheduler,
                detail_strength=float(preset.parameters["detail_strength"]),
            )
            return defaults, settings.max_candidates, preset.parameter_choices

    def configure(self, settings: RecordedSettings) -> None:
        self._project = self._project.configured(settings)

    def status(self, *, refresh: bool = True) -> SessionStatus:
        from datetime import UTC, datetime

        from pydantic import TypeAdapter

        from komaquill.adapters.atomic import atomic_json, read_bytes

        with api_operation(OperationStage.CHECK):
            path = self._project.state / "runtime-observation.json"
            adapter = TypeAdapter(SessionStatus)
            if refresh:
                result = inspection.inspect(self._project)
                result.observed_at = datetime.now(UTC)
                atomic_json(
                    path, adapter.dump_python(result, mode="json", exclude={"shutdown_target"})
                )
            else:
                try:
                    result = adapter.validate_json(read_bytes(path))
                except FileNotFoundError:
                    result = SessionStatus(gpu="unknown")
                except (OSError, ValueError):
                    result = SessionStatus(gpu="unknown", unreadable=[path.name])
            result.shutdown_target = inspection.shutdown_target(self._project)
            return result

    def views(self) -> list[dict[str, str]]:
        from urllib.parse import urlencode

        from komaquill.adapters.storage import load_session

        with api_operation(OperationStage.CHECK):
            if not self._project.session_file.is_file():
                return []
            session = load_session(self._project)
            settings = session.settings or self._project.settings()
            result = []
            for profile in settings.selected_profiles():
                for view in ("app", "graph"):
                    url = (
                        self._project.configuration.web.origin + "/comfy/"
                        if self._project.configuration.web.origin
                        else session.endpoint + "/"
                    )
                    if session.backend == "colab":
                        url += "?" + urlencode(
                            {"template": f"{profile}_{view}", "source": "komaquill"}
                        )
                    result.append(
                        {"profile": profile, "view": view, "url": url, "backend": session.backend}
                    )
            return result

    def workflow(self, profile: str, view: Literal["app", "graph"]) -> JsonObject:
        from komaquill.adapters.compute.connection import session_project
        from komaquill.adapters.workflows import prepare, select
        from komaquill.application.images.generation import generation_settings
        from komaquill.domain.images import compile_image_input
        from komaquill.domain.prompts import joined

        with api_operation(OperationStage.PREPARATION, invalid=ErrorCode.INVALID_CONFIGURATION):
            settings = session_project(self._project).settings()
            _, selected = select(settings, profile)
            wai_generate = selected.model_id == WAI_PROFILE and selected.operation == "generate"
            defaults = self._image_preferences() if wai_generate else ImagePromptDefaults()
            settings = generation_settings(settings, profile, wai=defaults.wai)
            selected = settings.presets[profile]
            negative = selected.negative_default
            applied = compile_image_input(
                selected,
                selected.sample_prompt,
                common_prompt=defaults.positive.strip(),
                negative=joined(negative, defaults.negative.strip())
                if negative is not None and defaults.negative.strip()
                else None,
            )
            randomize = wai_generate and defaults.wai.seed is None
            seed = randbelow(MAX_BROWSER_SEED + 1) if randomize else defaults.wai.seed
            _, graph = prepare(
                selected.sample_prompt,
                1 if seed is None else seed,
                "preview",
                settings=settings,
                profile_name=profile,
                compiled_input=applied,
                randomize_seed=randomize,
            )
            graph["extra"]["linearMode"] = view == "app"
            return graph

    def start(
        self,
        *,
        backend: Literal["colab", "external"] = "colab",
        endpoint: str | None = None,
        username: str | None = None,
        progress: Callable[[str], None] | None = None,
        interactive_auth: bool = False,
        foreground: Callable[[Path], int] | None = None,
        models: list[str] | None = None,
        gpu: GpuType | None = None,
        high_ram: bool | None = None,
        open_browser: bool = False,
    ) -> SessionStatus:
        with (
            operation_scope(),
            api_operation(OperationStage.PREPARATION, invalid=ErrorCode.INVALID_CONFIGURATION),
        ):
            project = self._project
            if backend == "external" and (gpu is not None or high_ram is not None):
                raise KomaquillError(
                    code="invalid_request",
                    stage="preparation",
                    display_key="session.hardware_colab_only",
                    retryable=False,
                )
            overrides = {
                key: value
                for key, value in {"models": models, "gpu": gpu, "high_ram": high_ram}.items()
                if value is not None
            }
            if overrides:
                from komaquill.domain.configuration import RuntimeSettings

                settings = RuntimeSettings.model_validate(
                    {**project.settings().model_dump(), **overrides}
                )
                project = project.configured(settings)
            supervisor.start(
                project,
                backend,
                endpoint,
                username,
                open_browser,
                progress or (lambda _: None),
                foreground,
                interactive_auth=interactive_auth,
            )
            return self.status()

    def usage(self) -> AccountUsage:
        """Observe current account consumption through the pinned provider adapter."""
        from komaquill.adapters.colab import Colab

        with api_operation(OperationStage.COLAB_CONTROL):
            return Colab(self._project).usage()

    def stop(
        self, *, force: bool = False, progress: Callable[[str], None] | None = None
    ) -> SessionStatus:
        echo = progress or (lambda _: None)
        with operation_scope(), api_operation(OperationStage.GPU_STOP):
            if not supervisor.stop(self._project, force, echo):
                shutdown.stop(self._project, force, echo)
            return self.status()
