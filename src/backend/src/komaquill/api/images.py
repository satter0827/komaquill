"""Compose image, session and evaluation use cases for Python and terminal callers."""

from collections.abc import Callable
from contextlib import suppress
from functools import cached_property
from hashlib import sha256
from pathlib import Path
from secrets import randbits
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from komaquill.adapters.image_text import ImageTextPreparation
from uuid import UUID

from komaquill.adapters.compute.connection import connected, session_project
from komaquill.adapters.diagnostics import operation_scope
from komaquill.adapters.materials import MaterialCollector
from komaquill.adapters.preparation import capabilities
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.api._errors import api_operation
from komaquill.application.images.generation import (
    Collection,
    collect,
    configured_generation_preset,
    generation_settings,
    submit_generation,
    wait_generation,
)
from komaquill.application.library.service import ImageDescriber
from komaquill.domain.configuration import RecordedSettings, RuntimeSettings
from komaquill.domain.errors import ErrorCode, KomaquillError, OperationStage
from komaquill.domain.images import (
    MAX_BROWSER_SEED,
    ImageGenerationConditions,
    ImagePurpose,
    PromptSource,
    preset_fingerprint,
)
from komaquill.domain.library.models import ImagePromptDefaults, WaiPreferences
from komaquill.domain.materials import InputArtifact
from komaquill.domain.models import Run
from komaquill.domain.presets import WAI_PROFILE
from komaquill.domain.prompts import joined


class ImageService:
    def __init__(
        self,
        project: RuntimeWorkspace,
        describer: ImageDescriber,
        preflight: Callable[[str, tuple[str, ...], str | None, str], None],
    ) -> None:
        self._preflight = preflight
        self._project = project
        self._describer = describer

    @cached_property
    def _text_preparation(self) -> "ImageTextPreparation":
        from komaquill.adapters.image_text import ImageTextPreparation

        return ImageTextPreparation(self._project.root, self._describer)

    @cached_property
    def _generation_workspace(self) -> RuntimeWorkspace:
        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            return self._project.configured()

    def configure(self, settings: RecordedSettings | None = None) -> None:
        with api_operation(OperationStage.CHECK):
            if settings is not None:
                self.__dict__["_generation_workspace"] = self._project.configured(settings)
            self._project = self._generation_workspace

    def supports_negative(
        self, model: str | None = None, operation: Literal["generate", "edit"] = "generate"
    ) -> bool:
        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            settings = session_project(self._generation_workspace).settings()
            return isinstance(settings, RuntimeSettings) and (
                settings.presets[settings.profile_for(model, operation)].negative_default
                is not None
            )

    def supports_pair(self) -> bool:
        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            settings = session_project(self._generation_workspace).settings()
            if not isinstance(settings, RuntimeSettings):
                return False
            return settings.presets[settings.profile_for(None)].model_id == WAI_PROFILE

    def validate_preferences(self, wai: WaiPreferences) -> None:
        with api_operation(OperationStage.CHECK, invalid=ErrorCode.INVALID_CONFIGURATION):
            settings = session_project(self._generation_workspace).settings()
            if not isinstance(settings, RuntimeSettings):
                raise KomaquillError(code="invalid_configuration", stage="check")
            configured_generation_preset(
                settings.presets[WAI_PROFILE], settings.max_candidates, wai=wai
            )

    def freeze(
        self, identity: UUID, parameters: dict[str, bool | int | float | str] | None
    ) -> Callable[[ImagePromptDefaults], ImageGenerationConditions]:
        """Capture the session defaults and resolve them with the atomically read preferences."""
        with api_operation(OperationStage.PREPARATION, invalid=ErrorCode.INVALID_CONFIGURATION):
            settings = session_project(self._generation_workspace).settings()
            if not isinstance(settings, RuntimeSettings):
                raise KomaquillError(code="invalid_configuration", stage="preparation")
            profile = settings.profile_for(None)
            preset = settings.presets[profile]
            assets = {name: settings.assets[name].reference for name in preset.asset_ids}

        def with_preferences(defaults: ImagePromptDefaults) -> ImageGenerationConditions:
            selected = configured_generation_preset(
                preset, settings.max_candidates, parameters=parameters, wai=defaults.wai
            )
            return ImageGenerationConditions(
                profile=profile,
                preset=selected,
                assets=assets,
                seed=self._seed(identity, selected.model_id == WAI_PROFILE, defaults.wai.seed),
            )

        return with_preferences

    @staticmethod
    def _seed(identity: UUID, wai_generate: bool, fixed: int | None) -> int:
        if wai_generate:
            return (
                fixed
                if fixed is not None
                else int.from_bytes(sha256(identity.bytes).digest()) & MAX_BROWSER_SEED
            )
        return randbits(63)

    def list(self) -> list[Run]:

        with api_operation(OperationStage.STORAGE):
            return [run for run in self._project.repository.load()]

    def get(self, run_id: UUID) -> Run:

        return self.read(run_id)

    def read(self, run_id: UUID) -> Run:
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
        purpose: ImagePurpose | None = None,
        image_prompts: ImagePromptDefaults | None = None,
        use_prompt_preferences: bool = False,
        conditions: ImageGenerationConditions | None = None,
    ) -> Run:
        from uuid import uuid4

        from filelock import FileLock

        from komaquill.adapters.persistence.images import reserve
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
                **({"purpose": purpose} if purpose else {}),
                **(
                    {
                        "accepted_conditions": {
                            "profile": conditions.profile,
                            "preset": preset_fingerprint(conditions.preset),
                            "assets": conditions.assets,
                            "seed": conditions.seed,
                        }
                    }
                    if conditions is not None
                    else {}
                ),
            }
        )
        with FileLock(str(self._generation_workspace.state / f".image-submit-{identity}.lock")):
            defaults = reserve(
                self._generation_workspace,
                identity,
                fingerprint,
                use_preferences=use_prompt_preferences,
                image_prompts=image_prompts,
            )
            if defaults is None:
                try:
                    return self.get(identity)
                except KomaquillError as error:
                    if error.code == "request_not_found":
                        raise KomaquillError(
                            code="submission_unknown", stage="submission", retryable=False
                        ) from None
                    raise
            try:
                with api_operation(OperationStage.GENERATION):
                    active_settings = session_project(self._generation_workspace).settings()
                    if not isinstance(active_settings, RuntimeSettings):
                        raise KomaquillError(code="invalid_configuration", stage="generation")
                    settings = active_settings
                    if conditions is None:
                        profile = settings.profile_for(model, operation)
                        settings = generation_settings(
                            settings, profile, candidates, parameters, wai=defaults.wai
                        )
                    else:
                        profile = conditions.profile
                        available = (
                            settings.presets.get(profile) if profile in settings.profiles else None
                        )
                        if (
                            available is None
                            or available.model_id != conditions.preset.model_id
                            or available.operation != conditions.preset.operation
                            or set(conditions.assets) != set(conditions.preset.asset_ids)
                            or any(
                                name not in settings.assets
                                or settings.assets[name].reference != reference
                                for name, reference in conditions.assets.items()
                            )
                        ):
                            raise KomaquillError(
                                code="operation_conflict", stage="preparation", retryable=False
                            )
                        settings = settings.model_copy(
                            update={"presets": {**settings.presets, profile: conditions.preset}}
                        )
                    selected = settings.presets[profile]
                    wai_generate = selected.model_id == WAI_PROFILE and operation == "generate"
                    selected_seed = conditions.seed if conditions is not None else seed
                    if selected_seed is None:
                        selected_seed = self._seed(identity, wai_generate, defaults.wai.seed)
                    common_positive = defaults.positive.strip()
                    common_negative = (
                        defaults.negative.strip() if selected.negative_default is not None else ""
                    )
                    requested_negative = (
                        joined(negative or "", common_negative) if common_negative else negative
                    )
                    all_subjects = (*subjects, common_positive) if common_positive else subjects
                    self._preflight(prompt, all_subjects, requested_negative, llm_profile)
                    descriptions = self._text_preparation.prepare(
                        identity,
                        prompt,
                        all_subjects,
                        requested_negative,
                        llm_profile,
                        allow_real_llm,
                    )
                    prepared_prompt = descriptions.prompt
                    common_prompt = descriptions.subjects[-1] if common_positive else ""
                    prepared_subjects = (
                        descriptions.subjects[:-1] if common_positive else descriptions.subjects
                    )
                    prepared_negative = descriptions.negative
                    if common_negative and negative is None and selected.negative_default:
                        prepared_negative = joined(
                            selected.negative_default, prepared_negative or ""
                        )
                    purpose_sources = []
                    if purpose:
                        positive = selected.purpose_prefixes.get(purpose, "")
                        negative_constraint = (
                            selected.purpose_negatives.get(purpose, "")
                            if selected.negative_default is not None
                            else ""
                        )
                        prepared_prompt = joined(positive, prepared_prompt)
                        if negative_constraint and selected.negative_default is not None:
                            prepared_negative = joined(
                                prepared_negative
                                if prepared_negative is not None
                                else selected.negative_default,
                                negative_constraint,
                            )
                        for role, content in (
                            ("positive", positive),
                            ("negative", negative_constraint),
                        ):
                            if content:
                                purpose_sources.append(
                                    PromptSource(
                                        name=f"purpose-{purpose}-{role}.txt",
                                        content=content,
                                        role=cast(Literal["positive", "negative"], role),
                                        sha256=sha256(content.encode()).hexdigest(),
                                    )
                                )
                    with connected(self._generation_workspace) as (backend, session):
                        if session.settings is not None and session.settings.model_dump(
                            mode="json"
                        ) != active_settings.model_dump(mode="json"):
                            raise KomaquillError(code="operation_conflict", stage="preparation")
                        return submit_generation(
                            self._generation_workspace,
                            backend,
                            session.model_copy(update={"settings": settings}),
                            prepared_prompt,
                            selected_seed,
                            profile,
                            run_id=identity,
                            negative=prepared_negative,
                            subjects=prepared_subjects,
                            common_prompt=common_prompt,
                            input_sources=(
                                *input_sources,
                                *descriptions.sources,
                                *purpose_sources,
                            ),
                            inputs=inputs,
                        )
            except Exception:
                from komaquill.adapters.persistence.images import cancel_unsubmitted

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
        purpose: ImagePurpose | None = None,
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
            purpose=purpose,
        )
        return self.wait(run.run_id)

    def wait(self, run_id: UUID) -> Run:
        with (
            api_operation(OperationStage.GENERATION),
            connected(self._generation_workspace) as (backend, session),
        ):
            result = wait_generation(self._generation_workspace, backend, session, run_id)
        from komaquill.adapters.creation_reconciliation import reconcile_creations

        reconcile_creations(self._project.root)
        return result

    def collect(self, run_id: UUID | None = None) -> Collection:
        with operation_scope(), api_operation(OperationStage.COLLECTION):
            if run_id is not None and self.read(run_id).state == "saved":
                return Collection(saved=1, pending=0, failed=0)
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
            from komaquill.adapters.creation_reconciliation import reconcile_creations

            reconcile_creations(self._project.root)
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
