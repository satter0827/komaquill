"""Keep work metadata and image visibility separate from immutable inputs."""

from uuid import UUID, uuid4

import pytest
from komaquill.adapters.persistence.schema import archived_conversations
from komaquill.api import (
    CharacterInput,
    Client,
    ConversationInput,
    CreationRequest,
    PresetSaveOptions,
    SceneRequest,
    SendInput,
    StoryInput,
)
from komaquill.domain.conversations.models import PresetSnapshot
from komaquill.domain.errors import KomaquillError
from komaquill.domain.library.works import ImageDetach, WorkChange, WorkRef
from komaquill.domain.models import Run


def saved_image(identity: UUID) -> Run:
    return Run(
        run_id=identity,
        runtime_id=uuid4(),
        state="saved",
        app_version="test",
        source_revision=None,
        remote_source_revision=None,
        comfy_revision=None,
        frontend_version=None,
        workflow_sha256="a" * 64,
        images=["cover.png", "candidate.png"],
    )


def test_rename_trash_and_restore_conversation(tmp_path):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(ConversationInput(title="Original title"))
    target = WorkRef(kind="conversation", id=conversation.id)
    renamed = api.works.change(
        WorkChange(
            target=target,
            action="rename",
            operation_id=uuid4(),
            expected_revision=0,
            name="New title",
        )
    )
    assert api.conversations.get(target.id).definition.title == "Original title"
    trash = WorkChange(
        target=target, action="trash", operation_id=uuid4(), expected_revision=renamed.revision
    )
    assert api.works.change(trash) == api.works.change(trash)
    assert api.conversations.list() == []
    with pytest.raises(KomaquillError, match="request_not_found"):
        api.conversations.get(target.id)
    (item,) = api.works.trash()
    assert item.name == "New title"
    api.works.change(
        WorkChange(
            target=target, action="restore", operation_id=uuid4(), expected_revision=item.revision
        )
    )
    assert api.conversations.get(target.id).definition.title == "Original title"
    assert api.works.get(target).name == "New title"
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.works.change(trash.model_copy(update={"action": "restore"}))
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.works.change(trash.model_copy(update={"operation_id": uuid4()}))


def test_image_output_requires_detachment_and_preserves_other_indices(tmp_path):
    api = Client.initialize(tmp_path)
    run = saved_image(uuid4())
    api._project.repository.save(run)
    character = api.library.store.create(
        "character", CharacterInput(name="Character"), image_id=run.run_id
    )
    story = api.stories.create(StoryInput(name="Story", character_id=character.id))
    conversation = api.conversations.create(
        ConversationInput(
            title="Retained character",
            presets={
                "character": PresetSnapshot(
                    id=character.id, revision=0, name=character.name, image_id=run.run_id
                )
            },
        )
    )
    target = WorkRef(kind="image", id=run.run_id)
    trash = WorkChange(target=target, action="trash", operation_id=uuid4(), expected_revision=0)
    with pytest.raises(KomaquillError, match="request_not_found"):
        api.works.get(target.model_copy(update={"index": 2}))
    with pytest.raises(KomaquillError, match="image_in_use"):
        api.works.change(trash)
    api.works.change(
        WorkChange(
            target=WorkRef(kind="conversation", id=conversation.id),
            action="rename",
            name="Renamed work",
            operation_id=uuid4(),
            expected_revision=0,
        )
    )
    cover, snapshot = api.works.image_uses(target)
    assert snapshot.name == "Renamed work"
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.works.detach(
            ImageDetach(image=target, use=snapshot.model_copy(update={"slot": "unknown"}))
        )
    api.works.detach(ImageDetach(image=target, use=cover))
    assert api.characters.get(character.id).image_id is None
    assert api.stories.get(story.id).character_id == character.id
    with pytest.raises(KomaquillError, match="image_in_use"):
        api.works.change(trash)
    detach = ImageDetach(image=target, use=snapshot)
    api.works.detach(detach)
    api.works.detach(detach)
    assert api.works.detached("conversation", conversation.id) == {f"character:{run.run_id}": [0]}
    assert (
        api.conversations.get(conversation.id).definition.presets["character"].image_id
        == run.run_id
    )
    item = api.works.change(trash)
    assert [item.index for item in api.works.image_outputs([run])[run.run_id]] == [1]
    assert api.images.get(run.run_id).images == ["cover.png", "candidate.png"]
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.library.store.create("character", CharacterInput(name="Copy"), image_id=run.run_id)
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.conversations.create(conversation.definition)
    api.works.change(
        WorkChange(
            target=target, action="restore", operation_id=uuid4(), expected_revision=item.revision
        )
    )
    assert [item.index for item in api.works.image_outputs([run])[run.run_id]] == [0, 1]


def test_story_edits_and_copies_preserve_an_existing_trashed_character(tmp_path):
    api = Client.initialize(tmp_path)
    character = api.characters.create(CharacterInput(name="Companion"))
    story = api.stories.create(StoryInput(name="Story", character_id=character.id))
    unrelated = api.stories.create(StoryInput(name="Unrelated"))
    api.characters.delete(character.id, character.revision)

    content = StoryInput(name="Edited story", character_id=character.id)
    edited = api.stories.update(story.id, story.revision, content)
    copied = api.stories.duplicate(edited.id)
    saved = api.library.save_preset(
        "story",
        content,
        PresetSaveOptions(
            operation_id=uuid4(), target_id=edited.id, expected_revision=edited.revision
        ),
    ).preset
    assert saved.character_id == edited.character_id == copied.character_id == character.id
    assert edited.name == copied.name == content.name and edited.id != copied.id
    with pytest.raises(KomaquillError, match="request_not_found"):
        api.stories.create(content)
    with pytest.raises(KomaquillError, match="request_not_found"):
        api.stories.update(unrelated.id, unrelated.revision, content)
    with pytest.raises(KomaquillError, match="request_not_found"):
        api.library.save_preset("story", content, PresetSaveOptions(operation_id=uuid4()))

    (trashed,) = api.works.trash()
    api.works.change(
        WorkChange(
            target=WorkRef(kind="character", id=character.id),
            operation_id=uuid4(),
            action="restore",
            expected_revision=trashed.revision,
        )
    )
    assert api.characters.get(api.stories.get(copied.id).character_id).id == character.id


def test_bookmark_name_and_visibility_follow_the_conversation(tmp_path):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(
        ConversationInput(title="Story", locale="en", llm_profile="fake")
    )
    operation = api.conversations.send(
        conversation.id, SendInput(operation_id=uuid4(), expected_revision=0, text="Hello")
    )
    bookmark = api.conversations.bookmark(
        conversation.id,
        SceneRequest(
            operation_id=uuid4(),
            expected_revision=api.conversations.overview(conversation.id).revision,
            turn_id=operation.id,
            name="Original",
        ),
    )
    ref = WorkRef(kind="bookmark", id=bookmark.id)
    changed = api.works.change(
        WorkChange(
            target=ref,
            action="rename",
            name="New bookmark",
            operation_id=uuid4(),
            expected_revision=0,
        )
    )
    assert changed.owner == conversation.id
    assert api.conversations.bookmarks(conversation.id)[0].name == "New bookmark"
    api.works.change(
        WorkChange(
            target=ref, action="trash", operation_id=uuid4(), expected_revision=changed.revision
        )
    )
    assert api.conversations.bookmarks(conversation.id) == []


def test_unreadable_archived_work_can_be_trashed_and_restored(tmp_path):
    api = Client.initialize(tmp_path)
    ref = WorkRef(kind="archived", id=uuid4())
    with api.conversations.repository.database.write() as connection:
        connection.execute(
            archived_conversations.insert().values(
                id=str(ref.id), payload="unreadable", reason="unreadable"
            )
        )
    assert not api.archive.conversations()[0].readable
    item = api.works.change(
        WorkChange(target=ref, action="trash", operation_id=uuid4(), expected_revision=0)
    )
    assert api.archive.conversations() == []
    api.works.change(
        WorkChange(
            target=ref, action="restore", operation_id=uuid4(), expected_revision=item.revision
        )
    )
    assert not api.archive.conversation(ref.id).readable


def test_completed_portrait_cannot_reattach_a_trashed_image(tmp_path):
    api = Client.initialize(tmp_path)
    character = api.characters.create(CharacterInput(name="Portrait"))
    request = CreationRequest(
        operation_id=uuid4(), kind="portrait", target_id=character.id, expected_revision=0
    )
    job, _ = api.library.store.accept(request, "{}")
    submitted = job.model_copy(update={"state": "submitted", "image_id": job.id})
    api.library.store.save_job(submitted)
    run = saved_image(job.id)
    api._project.repository.save(run)
    api.works.change(
        WorkChange(
            target=WorkRef(kind="image", id=run.run_id),
            operation_id=uuid4(),
            action="trash",
            expected_revision=0,
        )
    )
    completed = api.library.store.save_job(submitted.model_copy(update={"state": "complete"}))
    assert completed.image_id == run.run_id
    saved = api.characters.get(character.id)
    assert saved.image_id is None and saved.image_job_id is None


def test_saved_scene_is_protected_before_its_creation_job_is_reconciled(tmp_path):
    api = Client.initialize(tmp_path)
    conversation = api.conversations.create(ConversationInput(title="Scene"))
    request = CreationRequest(
        operation_id=uuid4(),
        kind="conversation_image",
        target_id=conversation.id,
        expected_revision=0,
    )
    job, _ = api.library.store.accept(request, "{}")
    run = saved_image(job.id)
    api._project.repository.save(run)
    target = WorkRef(kind="image", id=run.run_id)
    trash = WorkChange(target=target, action="trash", operation_id=uuid4(), expected_revision=0)
    with pytest.raises(KomaquillError, match="operation_busy"):
        api.works.change(trash)
    assert api.library.job(job.id).state == "complete"
    with pytest.raises(KomaquillError, match="image_in_use"):
        api.works.change(trash)
    (use,) = api.works.image_uses(target)
    api.works.detach(ImageDetach(image=target, use=use))
    assert api.works.change(trash).trashed
