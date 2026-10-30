"""Compose image, session and evaluation use cases for Python and terminal callers."""

import secrets
from contextlib import suppress
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from komaquill.adapters.image_text import ImageTextPreparation
from uuid import UUID

from komaquill.adapters.diagnostics import operation_scope
from komaquill.adapters.materials import MaterialCollector
from komaquill.adapters.preparation import capabilities
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.api._errors import api_operation
from komaquill.domain.configuration import RecordedSettings, RuntimeSettings
from komaquill.domain.errors import ErrorCode, KomaquillError, OperationStage
from komaquill.domain.images import PromptSource
from komaquill.domain.materials import InputArtifact
from komaquill.domain.models import Run
from komaquill.images.generation import Collection, collect, submit_generation, wait_generation
from komaquill.runtime.connection import connected


class ImageService:
    def __init__(self, project: RuntimeWorkspace) -> None:
        self._project = project

    @cached_property
    def _text_preparation(self) -> "ImageTextPreparation":
        from komaquill.adapters.image_text import ImageTextPreparation

        return ImageTextPreparation(self._project.root)

    @cached_property
    def _generation_workspace(self) -> RuntimeWorkspace:
        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            return self._project.configured()

    def configure(self, settings: RecordedSettings | None = None) -> None:
        with api_operation(OperationStage.CHECK):
            if settings is not None:
                self.__dict__["_generation_workspace"] = self._project.configured(settings)
            self._project = self._generation_workspace
            _ = self._text_preparation.responder.settings

    def list(self) -> list[Run]:
        with api_operation(OperationStage.STORAGE):
            return self._project.repository.load()

    def get(self, run_id: UUID) -> Run:
        with api_operation(OperationStage.STORAGE):
            return self._project.repository.read(run_id)

    def submit(
        self,
        prompt: str,
        *,
        run_id: UUID | None = None,
        seed: int | None = None,
        model: str | None = None,
        operation: Literal["generate", "edit"] = "generate",
        negative: str | None = None,
        subjects: tuple[str, ...] = (),
        candidates: int | None = None,
        input_sources: tuple[PromptSource, ...] = (),
        inputs: dict[str, InputArtifact] | None = None,
        parameters: dict[str, bool | int | float | str] | None = None,
        llm_profile: str = "fake",
        allow_real_llm: bool = False,
    ) -> Run:
        from uuid import uuid4

        from filelock import FileLock

        from komaquill.adapters.image_repository import reserve
        from komaquill.domain.workflow import digest

        identity = run_id or uuid4()
        self._generation_workspace.state.mkdir(parents=True, exist_ok=True)
        fingerprint = digest(
            {
                "prompt": prompt,
                "seed": seed,
                "model": model,
                "operation": operation,
                "negative": negative,
                "subjects": subjects,
                "candidates": candidates,
                "input_sources": [value.model_dump(mode="json") for value in input_sources],
                "inputs": {
                    key: value.model_dump(mode="json") for key, value in (inputs or {}).items()
                },
                "parameters": parameters,
                "llm_profile": llm_profile,
            }
        )
        with FileLock(str(self._generation_workspace.state / f".image-submit-{identity}.lock")):
            if not reserve(self._generation_workspace, identity, fingerprint):
                try:
                    return self.get(identity)
                except KomaquillError as error:
                    if error.code == "request_not_found":
                        raise KomaquillError(
                            code="submission_unknown", stage="submission", retryable=False
                        ) from None
                    raise
            try:
                descriptions = self._text_preparation.prepare(
                    identity, prompt, subjects, negative, llm_profile, allow_real_llm
                )
                with (
                    api_operation(OperationStage.GENERATION),
                    connected(self._generation_workspace) as (backend, session),
                ):
                    settings = session.settings or self._generation_workspace.settings()
                    if not isinstance(settings, RuntimeSettings):
                        raise KomaquillError(code="invalid_configuration", stage="generation")
                    profile = settings.profile_for(model, operation)
                    # Persist the deduplication key before any external submission.
                    return submit_generation(
                        self._generation_workspace,
                        backend,
                        session,
                        descriptions.prompt,
                        seed if seed is not None else secrets.randbits(63),
                        profile,
                        run_id=identity,
                        negative=descriptions.negative,
                        subjects=descriptions.subjects,
                        candidates=candidates,
                        input_sources=(*input_sources, *descriptions.sources),
                        inputs=inputs,
                        parameters=parameters,
                    )
            except Exception:
                from komaquill.adapters.image_repository import cancel_unsubmitted

                with suppress(KomaquillError):
                    cancel_unsubmitted(self._generation_workspace, identity)
                raise

    def generate(
        self,
        prompt: str,
        *,
        seed: int | None = None,
        model: str | None = None,
        operation: Literal["generate", "edit"] = "generate",
        negative: str | None = None,
        subjects: tuple[str, ...] = (),
        candidates: int | None = None,
        input_sources: tuple[PromptSource, ...] = (),
        inputs: dict[str, InputArtifact] | None = None,
        parameters: dict[str, bool | int | float | str] | None = None,
        llm_profile: str = "fake",
        allow_real_llm: bool = False,
    ) -> Run:
        run = self.submit(
            prompt,
            seed=seed,
            model=model,
            operation=operation,
            negative=negative,
            subjects=subjects,
            candidates=candidates,
            input_sources=input_sources,
            inputs=inputs,
            parameters=parameters,
            llm_profile=llm_profile,
            allow_real_llm=allow_real_llm,
        )
        with (
            api_operation(OperationStage.GENERATION),
            connected(self._generation_workspace) as (backend, session),
        ):
            return wait_generation(self._generation_workspace, backend, session, run.run_id)

    def collect(self, run_id: UUID | None = None) -> Collection:
        with operation_scope(), api_operation(OperationStage.COLLECTION):
            with connected(self._project) as (backend, session):
                result = collect(self._project, backend, session, run_id)
                if capabilities(backend):
                    material_errors = MaterialCollector(self._project, backend).collect()
                    result = Collection(
                        result.saved,
                        result.pending,
                        result.failed,
                        (*result.errors, *material_errors),
                        result.pending_ids,
                        observed_versions=result.observed_versions,
                    )
            return result


class InputService:
    def __init__(self, project: RuntimeWorkspace) -> None:
        self._project = project

    def import_file(
        self, path: str | Path, *, kind: Literal["image", "mask"] = "image"
    ) -> InputArtifact:
        from komaquill.adapters.inputs import import_file

        with api_operation(OperationStage.STORAGE):
            return import_file(self._project, Path(path), kind)

    def import_bytes(
        self, content: bytes, *, name: str, kind: Literal["image", "mask"] = "image"
    ) -> InputArtifact:
        from komaquill.adapters.inputs import import_bytes

        with api_operation(OperationStage.STORAGE):
            return import_bytes(self._project, content, name, kind)
