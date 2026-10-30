"""Bind typed library actions to the public client."""

from collections.abc import Callable
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, Response

from komaquill.api import Client
from komaquill.conversations.models import Conversation
from komaquill.library.models import (
    CharacterInput,
    CharacterPreset,
    ConversationStart,
    CreationJob,
    CreationRequest,
    PlayerInput,
    PlayerPreset,
    Preferences,
    StoryInput,
    StoryPreset,
)


def bind_library(app: FastAPI, selected: Callable[[], Client]) -> None:
    @app.get("/api/v1/preferences", response_model=Preferences, operation_id="get_preferences")
    def preferences() -> Preferences:
        return selected().library.preferences()

    @app.put("/api/v1/preferences", response_model=Preferences, operation_id="save_preferences")
    def save_preferences(body: Preferences) -> Preferences:
        return selected().library.save_preferences(body)

    @app.get("/api/v1/players", response_model=list[PlayerPreset], operation_id="list_players")
    def players() -> list[PlayerPreset]:
        return selected().players.list()

    @app.post(
        "/api/v1/players",
        response_model=PlayerPreset,
        status_code=201,
        operation_id="create_player",
    )
    def create_player(body: PlayerInput) -> PlayerPreset:
        return selected().players.create(body)

    @app.get("/api/v1/players/{identity}", response_model=PlayerPreset, operation_id="get_player")
    def player(identity: UUID) -> PlayerPreset:
        return selected().players.get(identity)

    @app.put(
        "/api/v1/players/{identity}", response_model=PlayerPreset, operation_id="update_player"
    )
    def update_player(identity: UUID, expected_revision: int, body: PlayerInput) -> PlayerPreset:
        return selected().players.update(identity, expected_revision, body)

    @app.delete("/api/v1/players/{identity}", status_code=204, operation_id="delete_player")
    def delete_player(identity: UUID, expected_revision: int) -> Response:
        selected().players.delete(identity, expected_revision)
        return Response(status_code=204)

    @app.post(
        "/api/v1/players/{identity}/duplicate",
        response_model=PlayerPreset,
        status_code=201,
        operation_id="duplicate_player",
    )
    def duplicate_player(identity: UUID) -> PlayerPreset:
        return selected().players.duplicate(identity)

    @app.get(
        "/api/v1/characters", response_model=list[CharacterPreset], operation_id="list_characters"
    )
    def characters() -> list[CharacterPreset]:
        return selected().characters.list()

    @app.post(
        "/api/v1/characters",
        response_model=CharacterPreset,
        status_code=201,
        operation_id="create_character",
    )
    def create_character(body: CharacterInput) -> CharacterPreset:
        return selected().characters.create(body)

    @app.get(
        "/api/v1/characters/{identity}",
        response_model=CharacterPreset,
        operation_id="get_character",
    )
    def character(identity: UUID) -> CharacterPreset:
        return selected().characters.get(identity)

    @app.put(
        "/api/v1/characters/{identity}",
        response_model=CharacterPreset,
        operation_id="update_character",
    )
    def update_character(
        identity: UUID, expected_revision: int, body: CharacterInput
    ) -> CharacterPreset:
        return selected().characters.update(identity, expected_revision, body)

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

    @app.post(
        "/api/v1/stories", response_model=StoryPreset, status_code=201, operation_id="create_story"
    )
    def create_story(body: StoryInput) -> StoryPreset:
        return selected().stories.create(body)

    @app.get("/api/v1/stories/{identity}", response_model=StoryPreset, operation_id="get_story")
    def story(identity: UUID) -> StoryPreset:
        return selected().stories.get(identity)

    @app.put("/api/v1/stories/{identity}", response_model=StoryPreset, operation_id="update_story")
    def update_story(identity: UUID, expected_revision: int, body: StoryInput) -> StoryPreset:
        return selected().stories.update(identity, expected_revision, body)

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
