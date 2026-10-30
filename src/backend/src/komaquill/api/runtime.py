"""Compose image, session and evaluation use cases for Python and terminal callers."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from komaquill.adapters.diagnostics import operation_scope
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.api._errors import api_operation
from komaquill.domain.configuration import GpuType, RecordedSettings
from komaquill.domain.errors import ErrorCode, KomaquillError, OperationStage
from komaquill.domain.models import JsonObject
from komaquill.domain.status import SessionStatus
from komaquill.domain.usage import AccountUsage
from komaquill.runtime import inspection, shutdown, supervisor


class RuntimeService:
    def __init__(self, project: RuntimeWorkspace) -> None:
        self._project = project

    def configure(self, settings: RecordedSettings) -> None:
        self._project = self._project.configured(settings)

    def status(self, *, refresh: bool = True) -> SessionStatus:
        from datetime import UTC, datetime

        from pydantic import TypeAdapter

        from komaquill.adapters.atomic import atomic_json, read_bytes

        with api_operation(OperationStage.CHECK):
            path = self._project.state / "runtime-observation.json"
            adapter = TypeAdapter(SessionStatus)
            if not refresh:
                return (
                    adapter.validate_json(read_bytes(path))
                    if path.is_file()
                    else SessionStatus(gpu="unknown")
                )
            result = inspection.inspect(self._project)
            result.observed_at = datetime.now(UTC)
            atomic_json(path, adapter.dump_python(result, mode="json"))
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
                    url = session.endpoint + "/"
                    if session.backend == "colab":
                        url += "?" + urlencode(
                            {"template": f"{profile}_{view}", "source": "komaquill"}
                        )
                    result.append(
                        {"profile": profile, "view": view, "url": url, "backend": session.backend}
                    )
            return result

    def workflow(self, profile: str, view: Literal["app", "graph"]) -> JsonObject:
        from komaquill.adapters.workflows import prepare, select
        from komaquill.runtime.connection import session_project

        with api_operation(OperationStage.PREPARATION, invalid=ErrorCode.INVALID_CONFIGURATION):
            settings = session_project(self._project).settings()
            _, selected = select(settings, profile)
            _, graph = prepare(
                selected.sample_prompt, 1, "preview", settings=settings, profile_name=profile
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
