"""Bind typed library actions to the public client."""

from collections.abc import Callable
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, Response

from komaquill.adapters.conversation_archive import ArchivedConversation
from komaquill.api import Client
from komaquill.conversations.models import Conversation
from komaquill.library.models import (
    CharacterPreset,
    CharacterSaveRequest,
    ConversationImageRequest,
    ConversationStart,
    CreationJob,
    CreationRequest,
    PlayerInput,
    PlayerProfile,
    Preferences,
    PresetSaveOptions,
    PresetSaveResult,
    StoryPreset,
    StorySaveRequest,
)


def bind_library(app: FastAPI, selected: Callable[[], Client]) -> None:
    @app.get(
        "/api/v1/archive/conversations",
        response_model=list[ArchivedConversation],
        operation_id="list_archived_conversations",
    )
    def archived_conversations() -> list[ArchivedConversation]:
        return selected().archive.conversations()

    @app.get(
        "/api/v1/archive/conversations/{identity}",
        response_model=ArchivedConversation,
        operation_id="get_archived_conversation",
    )
    def archived_conversation(identity: UUID) -> ArchivedConversation:
        return selected().archive.conversation(identity)

    @app.get(
        "/api/v1/conversations/{identity}/images",
        response_model=list[CreationJob],
        operation_id="list_conversation_images",
    )
    def conversation_images(identity: UUID) -> list[CreationJob]:
        return selected().library.conversation_images(identity)

    @app.post(
        "/api/v1/conversations/{identity}/image",
        response_model=CreationJob,
        status_code=202,
        operation_id="draw_conversation",
    )
    def draw_conversation(
        identity: UUID, body: ConversationImageRequest, tasks: BackgroundTasks
    ) -> CreationJob:
        client = selected()
        conversation = client.conversations.get(identity)
        job, created = client.library.accept(
            CreationRequest(
                operation_id=body.operation_id,
                kind="conversation_image",
                target_id=identity,
                expected_revision=body.expected_revision,
                locale=body.locale,
                llm_profile=conversation.definition.llm_profile,
                allow_real_llm=True,
            )
        )
        if created:
            tasks.add_task(client.library.execute, job.id)
        return job

    @app.get("/api/v1/preferences", response_model=Preferences, operation_id="get_preferences")
    def preferences() -> Preferences:
        return selected().library.preferences()

    @app.put("/api/v1/preferences", response_model=Preferences, operation_id="save_preferences")
    def save_preferences(body: Preferences) -> Preferences:
        return selected().library.save_preferences(body)

    @app.get("/api/v1/player", response_model=PlayerProfile, operation_id="get_player")
    def player() -> PlayerProfile:
        return selected().player.get()

    @app.put("/api/v1/player", response_model=PlayerProfile, operation_id="update_player")
    def update_player(expected_revision: int, body: PlayerInput) -> PlayerProfile:
        return selected().player.update(expected_revision, body)

    @app.get(
        "/api/v1/characters", response_model=list[CharacterPreset], operation_id="list_characters"
    )
    def characters() -> list[CharacterPreset]:
        return selected().characters.list()

    @app.post(
        "/api/v1/characters/save", response_model=PresetSaveResult, operation_id="save_character"
    )
    def save_character(body: CharacterSaveRequest, tasks: BackgroundTasks) -> PresetSaveResult:
        result = selected().library.save_preset(
            "character",
            body.value,
            PresetSaveOptions.model_validate(body.model_dump(exclude={"value"})),
            execute=False,
        )
        if result.job is not None:
            tasks.add_task(selected().library.execute, result.job.id)
        return result

    @app.get(
        "/api/v1/characters/{identity}",
        response_model=CharacterPreset,
        operation_id="get_character",
    )
    def character(identity: UUID) -> CharacterPreset:
        return selected().characters.get(identity)

    @app.delete("/api/v1/characters/{identity}", status_code=204, operation_id="delete_character")
    def delete_character(identity: UUID, expected_revision: int) -> Response:
        selected().characters.delete(identity, expected_revision)
        return Response(status_code=204)

    @app.post(
        "/api/v1/characters/{identity}/duplicate",
        response_model=CharacterPreset,
        status_code=201,
        operation_id="duplicate_character",
    )
    def duplicate_character(identity: UUID) -> CharacterPreset:
        return selected().characters.duplicate(identity)

    @app.get("/api/v1/stories", response_model=list[StoryPreset], operation_id="list_stories")
    def stories() -> list[StoryPreset]:
        return selected().stories.list()

    @app.post("/api/v1/stories/save", response_model=PresetSaveResult, operation_id="save_story")
    def save_story(body: StorySaveRequest, tasks: BackgroundTasks) -> PresetSaveResult:
        result = selected().library.save_preset(
            "story",
            body.value,
            PresetSaveOptions.model_validate(body.model_dump(exclude={"value"})),
            execute=False,
        )
        if result.job is not None:
            tasks.add_task(selected().library.execute, result.job.id)
        return result

    @app.get("/api/v1/stories/{identity}", response_model=StoryPreset, operation_id="get_story")
    def story(identity: UUID) -> StoryPreset:
        return selected().stories.get(identity)

    @app.delete("/api/v1/stories/{identity}", status_code=204, operation_id="delete_story")
    def delete_story(identity: UUID, expected_revision: int) -> Response:
        selected().stories.delete(identity, expected_revision)
        return Response(status_code=204)

    @app.post(
        "/api/v1/stories/{identity}/duplicate",
        response_model=StoryPreset,
        status_code=201,
        operation_id="duplicate_story",
    )
    def duplicate_story(identity: UUID) -> StoryPreset:
        return selected().stories.duplicate(identity)

    @app.post(
        "/api/v1/conversations/from-presets",
        response_model=Conversation,
        status_code=201,
        operation_id="start_conversation",
    )
    def start_conversation(body: ConversationStart) -> Conversation:
        return selected().library.start(body)

    @app.post(
        "/api/v1/creation-jobs",
        response_model=CreationJob,
        status_code=202,
        operation_id="create_job",
    )
    def create_job(body: CreationRequest, tasks: BackgroundTasks) -> CreationJob:
        job, created = selected().library.accept(body)
        if created:
            tasks.add_task(selected().library.execute, job.id)
        return job

    @app.get("/api/v1/creation-jobs/{identity}", response_model=CreationJob, operation_id="get_job")
    def job(identity: UUID) -> CreationJob:
        return selected().library.job(identity)
