"""Compose reusable settings, frozen conversation definitions and creation work."""

from __future__ import annotations

import builtins
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel

from komaquill.application.conversations.service import ConversationService
from komaquill.application.library.operations import CreationStore, reconcile_job, reconcile_jobs
from komaquill.domain.conversations.context import drawing_context
from komaquill.domain.conversations.models import Conversation, ConversationInput, PresetSnapshot
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import ImagePurpose
from komaquill.domain.library.models import (
    CharacterInput,
    CharacterPreset,
    ConversationStart,
    CreationJob,
    CreationRequest,
    PlayerInput,
    PlayerProfile,
    Preferences,
    Preset,
    PresetInput,
    PresetKind,
    PresetSaveOptions,
    PresetSaveResult,
    StoryInput,
    StoryPreset,
)
from komaquill.domain.library.rendering import preset_image_input
from komaquill.domain.models import Run
from komaquill.domain.roleplay import CharacterDefinition, PersonDefinition, StoryDefinition

IMAGE_COMPOSITION = {
    "portrait": "Solo character portrait, single figure, simple background. ",
    "conversation_image": "Solo character, single figure, first-person viewpoint. ",
    "banner": "Unoccupied setting, empty foreground, wide establishing view. ",
}


class Store(CreationStore, Protocol):
    def initialize(self, values: list[tuple[PresetKind, Preset]]) -> None: ...
    def list(self, kind: PresetKind) -> list[Preset]: ...
    def get(self, kind: PresetKind, identity: UUID) -> Preset: ...
    def resolve(
        self, references: dict[PresetKind, tuple[UUID, int]]
    ) -> dict[PresetKind, Preset]: ...
    def create(
        self,
        kind: PresetKind,
        value: PresetInput,
        *,
        image_id: UUID | None = None,
        image_stale: bool = False,
    ) -> Preset: ...
    def update(
        self, kind: PresetKind, identity: UUID, revision: int, value: PresetInput
    ) -> Preset: ...
    def delete(self, kind: PresetKind, identity: UUID, revision: int) -> None: ...
    def attach(
        self, kind: PresetKind, identity: UUID, revision: int, job: UUID, image: UUID | None = None
    ) -> bool: ...
    def preferences(self) -> Preferences: ...
    def save_preferences(self, value: Preferences) -> Preferences: ...
    def retain_job(self, value: CreationJob) -> None: ...
    def accept(
        self, request: CreationRequest, original: str, *, turn_id: UUID | None = None
    ) -> tuple[CreationJob, bool]: ...
    def save_preset(
        self,
        kind: PresetKind,
        value: PresetInput,
        options: PresetSaveOptions,
    ) -> PresetSaveResult: ...
    def execution(self, identity: UUID) -> AbstractContextManager[CreationJob | None]: ...
    def conversation_images(self, identity: UUID) -> builtins.list[CreationJob]: ...


class Creator(Protocol):
    def draft(self, request: CreationRequest) -> CharacterInput | StoryInput: ...


class ImageDescriber(Protocol):
    def prepare_image(
        self,
        text: str,
        operation: UUID,
        profile: str,
        allow_real_llm: bool,
        *,
        compose: bool = False,
        purpose: str = "image",
    ) -> str: ...


class Images(Protocol):
    def supports_pair(self) -> bool: ...
    def read(self, run_id: UUID) -> Run: ...
    def submit(
        self,
        prompt: str,
        *,
        run_id: UUID | None = None,
        parameters: dict[str, bool | int | float | str] | None = None,
        purpose: ImagePurpose | None = None,
    ) -> Run: ...
    def get(self, run_id: UUID) -> Run: ...


class PresetService[Input: BaseModel, Output: BaseModel]:
    def __init__(
        self, store: Store, kind: PresetKind, input_type: type[Input], output_type: type[Output]
    ) -> None:
        self.store, self.kind = store, kind
        self.input_type, self.output_type = input_type, output_type

    def list(self) -> list[Output]:
        return [self.output_type.model_validate(value) for value in self.store.list(self.kind)]

    def get(self, identity: UUID) -> Output:
        return self.output_type.model_validate(self.store.get(self.kind, identity))

    def create(self, value: Input) -> Output:
        self._validate(value)
        return self.output_type.model_validate(
            self.store.create(self.kind, cast(PresetInput, value))
        )

    def update(self, identity: UUID, revision: int, value: Input) -> Output:
        self._validate(value)
        return self.output_type.model_validate(
            self.store.update(self.kind, identity, revision, cast(PresetInput, value))
        )

    def duplicate(self, identity: UUID) -> Output:
        value = self.get(identity)
        fields = self.input_type.model_fields
        content = self.input_type.model_validate(value.model_dump(include=set(fields)))
        self._validate(content)
        return self.output_type.model_validate(
            self.store.create(
                self.kind,
                cast(PresetInput, content),
                image_id=cast(Preset, value).image_id,
                image_stale=cast(Preset, value).image_stale,
            )
        )

    def delete(self, identity: UUID, revision: int) -> None:
        self.store.delete(self.kind, identity, revision)

    def _validate(self, value: Input) -> None:
        if isinstance(value, StoryInput) and value.character_id is not None:
            self.store.get("character", value.character_id)


class PlayerService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self) -> PlayerProfile:
        values = self.store.list("player")
        if len(values) != 1:
            raise KomaquillError(code="storage_failed", retryable=False)
        return PlayerProfile.model_validate(values[0])

    def update(self, revision: int, value: PlayerInput) -> PlayerProfile:
        return PlayerProfile.model_validate(
            self.store.update("player", self.get().id, revision, value)
        )


def sample_id(locale: str, kind: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"komaquill:sample:{locale}:{kind}")


class LibraryService:
    def __init__(
        self,
        store: Store,
        conversations: ConversationService,
        creator: Creator,
        image_describer: ImageDescriber,
        images: Images,
        samples: dict[str, dict[str, dict[str, object]]],
        image_parameters: dict[str, dict[str, bool | int | float | str]],
        preflight: Callable[[CreationRequest, str], None],
        image_lookup: Callable[[UUID], Run] | None = None,
    ) -> None:
        self.store, self.conversations, self.creator, self.images = (
            store,
            conversations,
            creator,
            images,
        )
        self.preflight = preflight
        self.image_parameters = image_parameters
        self.image_lookup = image_lookup or images.get
        self.image_describer = image_describer
        self.player = PlayerService(store)
        self.characters = PresetService(store, "character", CharacterInput, CharacterPreset)
        self.stories = PresetService(store, "story", StoryInput, StoryPreset)
        values: list[tuple[PresetKind, Preset]] = []
        models: dict[
            PresetKind, type[PlayerProfile] | type[CharacterPreset] | type[StoryPreset]
        ] = {
            "player": PlayerProfile,
            "character": CharacterPreset,
            "story": StoryPreset,
        }
        for locale, definitions in samples.items():
            for kind, model in models.items():
                if kind == "player" and locale != "ja":
                    continue
                data = dict(definitions[kind])
                if kind == "story":
                    data["character_id"] = sample_id(locale, "character")
                value = model.model_validate(
                    {**data, "id": sample_id(locale, kind), "sample": True, "sample_locale": locale}
                )
                values.append((kind, value))
        self._initial_presets = values

    def initialize(self) -> None:
        self.store.initialize(self._initial_presets)

    def preferences(self) -> Preferences:
        return self.store.preferences()

    def conversation_images(self, identity: UUID) -> builtins.list[CreationJob]:
        self.conversations.get(identity)
        return [self.job(job.id) for job in self.store.conversation_images(identity)]

    def save_preferences(self, value: Preferences) -> Preferences:
        if value.llm_profile != self.preferences().llm_profile and value.llm_profile not in {
            p.id for p in self.conversations.profiles()
        }:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        return self.store.save_preferences(value)

    def start(self, request: ConversationStart) -> Conversation:
        player = self.player.get()
        preferences = self.preferences()
        profile = request.llm_profile or preferences.llm_profile
        values = self.store.resolve(
            {
                "player": (player.id, request.player_revision),
                "character": (request.character.id, request.character.revision),
                "story": (request.story.id, request.story.revision),
            }
        )
        sample = (
            all(
                v.sample and v.id == sample_id(request.locale, k)
                for k, v in values.items()
                if k != "player"
            )
            and player.sample
        )
        if profile == "fake" and not sample:
            raise KomaquillError(code="fake_input_mismatch", retryable=False)
        story = cast(StoryPreset, values["story"])

        return self.conversations.create(
            ConversationInput(
                title=request.title.strip() or story.name,
                tone=request.tone,
                topics=request.topics,
                avoid=request.avoid,
                scenario=StoryDefinition.model_validate(
                    story.model_dump(include=set(StoryDefinition.model_fields))
                ),
                character=CharacterDefinition.model_validate(
                    values["character"].model_dump(include=set(CharacterDefinition.model_fields))
                ),
                user=PersonDefinition.model_validate(
                    values["player"].model_dump(include=set(PersonDefinition.model_fields))
                ),
                locale=request.locale,
                llm_profile=profile,
                response_length=preferences.response_length,
                sample=sample,
                presets={
                    key: PresetSnapshot(
                        id=v.id, revision=v.revision, name=v.name, image_id=v.image_id
                    )
                    for key, v in values.items()
                },
            )
        )

    def save_preset(
        self,
        kind: PresetKind,
        value: PresetInput,
        options: PresetSaveOptions,
    ) -> PresetSaveResult:
        if kind not in {"character", "story"} or not isinstance(
            value, CharacterInput if kind == "character" else StoryInput
        ):
            raise KomaquillError(code="invalid_request", retryable=False)
        return self.store.save_preset(kind, value, options)

    def reconcile(self) -> list[UUID]:
        return reconcile_jobs(self.store, self.image_lookup)

    def accept(self, request: CreationRequest) -> tuple[CreationJob, bool]:
        try:
            previous, _ = self.store.job(request.operation_id)
        except KomaquillError as error:
            if error.code != "request_not_found":
                raise
        else:
            if previous.request != request:
                raise KomaquillError(code="operation_conflict", retryable=False)
            return self.job(previous.id), False
        original, turn_id = self.source(request)
        self.preflight(request, original)
        job, created = self.store.accept(request, original, turn_id=turn_id)
        return job, created

    def source(self, request: CreationRequest) -> tuple[str, UUID | None]:
        original = request.prompt
        turn_id = None
        if request.kind == "conversation_image":
            if request.subjects == "pair" and not self.images.supports_pair():
                raise KomaquillError(code="image_composition_unavailable", retryable=False)
            if request.target_id is None:
                raise KomaquillError(code="invalid_request", retryable=False)
            conversation = self.conversations.get(request.target_id)
            if conversation.revision != request.expected_revision:
                raise KomaquillError(code="operation_conflict", retryable=False)
            turn_id = conversation.turns[-1].operation_id if conversation.turns else None
            original = json.dumps(
                drawing_context(conversation, request.subjects, request.viewpoint),
                ensure_ascii=False,
            )
        elif request.kind in {"portrait", "banner"}:
            try:
                self.images.get(request.operation_id)
            except KomaquillError as error:
                if error.code != "request_not_found":
                    raise
            else:
                raise KomaquillError(code="operation_conflict", retryable=False)
            if request.target_id is None or request.expected_revision is None:
                raise KomaquillError(code="invalid_request", retryable=False)
            kind: PresetKind = "character" if request.kind == "portrait" else "story"
            value = self.store.get(kind, request.target_id)
            if isinstance(value, CharacterPreset) and value.visual_review_required:
                raise KomaquillError(code="visual_review_required", retryable=False)
            if value.revision != request.expected_revision:
                raise KomaquillError(code="operation_conflict", retryable=False)
            original = json.dumps(preset_image_input(kind, value), ensure_ascii=False)
        elif (
            not original.strip()
            or request.target_id is not None
            or request.expected_revision is not None
        ):
            raise KomaquillError(code="invalid_request", retryable=False)
        return original, turn_id

    def execute(self, identity: UUID) -> CreationJob:
        with self.store.execution(identity) as job:
            return self._execute(job) if job is not None else self.job(identity)

    def _execute(self, job: CreationJob) -> CreationJob:
        try:
            if job.request.kind.endswith("_draft"):
                result = self.creator.draft(job.request)
                job = job.model_copy(update={"state": "complete", "result": result})
            else:
                source = job.original
                if job.request.kind == "banner":
                    captured = json.loads(source)
                    scenery = {
                        key: captured[key]
                        for key in ("name", "world", "place", "description")
                        if captured.get(key)
                    }
                    if not any(scenery.get(key) for key in ("world", "place", "description")):
                        scenery["opening"] = captured.get("opening", "")
                    source = json.dumps(scenery, ensure_ascii=False)
                environment_only = job.request.kind == "conversation_image" and not json.loads(
                    source
                ).get("character_visible", True)
                image_purpose = (
                    "banner" if environment_only else cast(ImagePurpose, job.request.kind)
                )
                prompt = job.prompt_en
                if prompt is None:
                    composition = (
                        "Unoccupied setting, environment only. "
                        if environment_only
                        else (
                            (
                                "Two distinct people, observer viewpoint. "
                                if job.request.viewpoint == "observer"
                                else (
                                    "Two distinct people, first-person viewpoint from the player; "
                                    "the player may be partially visible. "
                                )
                            )
                            if job.request.subjects == "pair"
                            else (
                                "Solo character, observer viewpoint. "
                                if job.request.viewpoint == "observer"
                                else IMAGE_COMPOSITION[job.request.kind]
                            )
                        )
                    )
                    prompt = composition + self.image_describer.prepare_image(
                        source,
                        job.id,
                        job.request.llm_profile,
                        job.request.allow_real_llm,
                        compose=True,
                        purpose=cast(ImagePurpose, job.request.kind),
                    )
                job = job.model_copy(update={"prompt_en": prompt})
                self.store.save_job(job)
                self.images.submit(
                    prompt,
                    run_id=job.id,
                    parameters=self.image_parameters.get(image_purpose),
                    purpose=image_purpose,
                )
                job = job.model_copy(update={"state": "submitted", "image_id": job.id})
        except KomaquillError as error:
            if error.code == "submission_unknown" and not job.request.kind.endswith("_draft"):
                job = job.model_copy(
                    update={"state": "submitted", "image_id": job.id, "error": error.code.value}
                )
            else:
                job = job.model_copy(
                    update={
                        "state": "failed",
                        "error": error.code.value,
                        "error_stage": error.stage,
                    }
                )
        except Exception:
            job = job.model_copy(update={"state": "failed", "error": "operation_failed"})
        if job.state == "complete" and job.request.kind.endswith("_draft"):
            self.store.retain_job(job)
        self.store.save_job(job)
        return self.job(job.id)

    def job(self, identity: UUID) -> CreationJob:
        job = reconcile_job(self.store, self.image_lookup, identity)
        return job
