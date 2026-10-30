"""Compose reusable settings, frozen conversation definitions and creation work."""

import json
from typing import Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel

from komaquill.conversations.models import Conversation, ConversationInput, PresetSnapshot
from komaquill.conversations.service import ConversationService
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import Run
from komaquill.library.models import (
    CharacterInput,
    CharacterPreset,
    ConversationStart,
    CreationJob,
    CreationRequest,
    PlayerInput,
    PlayerPreset,
    Preferences,
    Preset,
    PresetInput,
    PresetKind,
    StoryInput,
    StoryPreset,
)


class Store(Protocol):
    def initialize(self, values: list[tuple[PresetKind, Preset]], player: UUID) -> None: ...
    def list(self, kind: PresetKind) -> list[Preset]: ...
    def get(self, kind: PresetKind, identity: UUID) -> Preset: ...
    def resolve(
        self, references: dict[PresetKind, tuple[UUID, int]]
    ) -> dict[PresetKind, Preset]: ...
    def create(
        self, kind: PresetKind, value: PresetInput, *, image_id: UUID | None = None
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
    def accept(self, request: CreationRequest, original: str) -> tuple[CreationJob, bool]: ...
    def job(self, identity: UUID) -> tuple[CreationJob, bool]: ...
    def claim(self, identity: UUID) -> CreationJob | None: ...
    def save_job(self, value: CreationJob) -> CreationJob: ...


class Creator(Protocol):
    def draft(self, request: CreationRequest) -> CharacterInput | StoryInput: ...
    def prepare_image(
        self, text: str, operation: UUID, profile: str, allow_real_llm: bool
    ) -> str: ...


class Images(Protocol):
    def submit(
        self,
        prompt: str,
        *,
        run_id: UUID | None = None,
        parameters: dict[str, bool | int | float | str] | None = None,
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
                self.kind, cast(PresetInput, content), image_id=cast(Preset, value).image_id
            )
        )

    def delete(self, identity: UUID, revision: int) -> None:
        self.store.delete(self.kind, identity, revision)

    def _validate(self, value: Input) -> None:
        if isinstance(value, StoryInput) and value.character_id is not None:
            self.store.get("character", value.character_id)


def sample_id(locale: str, kind: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"komaquill:sample:{locale}:{kind}")


class LibraryService:
    def __init__(
        self,
        store: Store,
        conversations: ConversationService,
        creator: Creator,
        images: Images,
        samples: dict[str, dict[str, dict[str, object]]],
        image_parameters: dict[str, dict[str, bool | int | float | str]],
    ) -> None:
        self.store, self.conversations, self.creator, self.images = (
            store,
            conversations,
            creator,
            images,
        )
        self.image_parameters = image_parameters
        self.players = PresetService(store, "player", PlayerInput, PlayerPreset)
        self.characters = PresetService(store, "character", CharacterInput, CharacterPreset)
        self.stories = PresetService(store, "story", StoryInput, StoryPreset)
        values: list[tuple[PresetKind, Preset]] = []
        models: dict[PresetKind, type[PlayerPreset] | type[CharacterPreset] | type[StoryPreset]] = {
            "player": PlayerPreset,
            "character": CharacterPreset,
            "story": StoryPreset,
        }
        for locale, definitions in samples.items():
            for kind, model in models.items():
                data = dict(definitions[kind])
                if kind == "story":
                    data["character_id"] = sample_id(locale, "character")
                value = model.model_validate(
                    {**data, "id": sample_id(locale, kind), "sample": True, "sample_locale": locale}
                )
                values.append((kind, value))
        store.initialize(values, sample_id("ja", "player"))

    def preferences(self) -> Preferences:
        return self.store.preferences()

    def save_preferences(self, value: Preferences) -> Preferences:
        if value.llm_profile not in {p.id for p in self.conversations.profiles()}:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        if value.player_id:
            self.players.get(value.player_id)
        return self.store.save_preferences(value)

    def start(self, request: ConversationStart) -> Conversation:
        values = self.store.resolve(
            {
                "player": (request.player.id, request.player.revision),
                "character": (request.character.id, request.character.revision),
                "story": (request.story.id, request.story.revision),
            }
        )
        sample = all(v.sample and v.id == sample_id(request.locale, k) for k, v in values.items())
        if request.llm_profile == "fake" and not sample:
            raise KomaquillError(code="fake_input_mismatch", retryable=False)
        story = cast(StoryPreset, values["story"])

        def content(value: Preset) -> str:
            return json.dumps(
                value.model_dump(
                    mode="json",
                    exclude={
                        "id",
                        "revision",
                        "image_id",
                        "image_job_id",
                        "sample",
                        "sample_locale",
                        "character_id",
                    },
                ),
                ensure_ascii=False,
            )

        return self.conversations.create(
            ConversationInput(
                title=request.title.strip() or story.name,
                scenario=content(story),
                character=content(values["character"]),
                user=content(values["player"]),
                initial_state={"place": story.place} if story.place else {},
                locale=request.locale,
                images=request.images,
                llm_profile=request.llm_profile,
                response_length=self.preferences().response_length,
                sample=sample,
                presets={
                    key: PresetSnapshot(
                        id=v.id, revision=v.revision, name=v.name, image_id=v.image_id
                    )
                    for key, v in values.items()
                },
            )
        )

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
        original = request.prompt
        if request.kind in {"portrait", "banner"}:
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
            if value.revision != request.expected_revision:
                raise KomaquillError(code="operation_conflict", retryable=False)
            original = json.dumps(
                value.model_dump(
                    mode="json",
                    exclude={
                        "id",
                        "revision",
                        "image_id",
                        "image_job_id",
                        "sample",
                        "sample_locale",
                        "character_id",
                    },
                ),
                ensure_ascii=False,
            )
        elif (
            not original.strip()
            or request.target_id is not None
            or request.expected_revision is not None
        ):
            raise KomaquillError(code="invalid_request", retryable=False)
        job, created = self.store.accept(request, original)
        if created and request.target_id is not None and request.expected_revision is not None:
            kind = "character" if request.kind == "portrait" else "story"
            self.store.attach(kind, request.target_id, request.expected_revision, job.id)
        return job, created

    def execute(self, identity: UUID) -> CreationJob:
        job = self.store.claim(identity)
        if job is None:
            return self.job(identity)
        try:
            if job.request.kind.endswith("_draft"):
                result = self.creator.draft(job.request)
                job = job.model_copy(update={"state": "complete", "result": result})
            else:
                prefix = (
                    "Portrait of one character. "
                    if job.request.kind == "portrait"
                    else ("Wide establishing view of the story setting, no lettering or text. ")
                )
                prompt = self.creator.prepare_image(
                    prefix + job.original,
                    job.id,
                    job.request.llm_profile,
                    job.request.allow_real_llm,
                )
                job = job.model_copy(update={"prompt_en": prompt})
                self.store.save_job(job)
                self.images.submit(
                    prompt, run_id=job.id, parameters=self.image_parameters[job.request.kind]
                )
                job = job.model_copy(update={"state": "submitted", "image_id": job.id})
        except KomaquillError as error:
            job = job.model_copy(update={"state": "failed", "error": error.code.value})
        except Exception:
            job = job.model_copy(update={"state": "failed", "error": "operation_failed"})
        self.store.save_job(job)
        return self.job(identity)

    def job(self, identity: UUID) -> CreationJob:
        job, alive = self.store.job(identity)
        observed = job
        if job.state in {"accepted", "running", "submitted"} and not job.request.kind.endswith(
            "_draft"
        ):
            try:
                run = self.images.get(job.id)
            except KomaquillError as error:
                if error.code != "request_not_found":
                    raise
            else:
                if run.state == "saved" and run.images:
                    job = job.model_copy(update={"state": "complete", "image_id": run.run_id})
                    request = job.request
                    if request.target_id is not None and request.expected_revision is not None:
                        kind: PresetKind = "character" if request.kind == "portrait" else "story"
                        self.store.attach(
                            kind, request.target_id, request.expected_revision, job.id, run.run_id
                        )
                elif run.state in {"failed", "lost"}:
                    job = job.model_copy(update={"state": "failed", "error": "generation_failed"})
                elif run.state != "prepared":
                    job = job.model_copy(update={"state": "submitted", "image_id": run.run_id})
        if not alive and job.state in {"accepted", "running"}:
            job = job.model_copy(update={"state": "interrupted", "error": "submission_unknown"})
        return self.store.save_job(job) if job != observed else self.store.job(identity)[0]
