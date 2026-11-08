"""Keep work metadata and image visibility separate from immutable inputs."""

from uuid import uuid4

import pytest
from komaquill.api import CharacterInput, Client, ConversationInput, StoryInput
from komaquill.domain.errors import KomaquillError
from komaquill.domain.library.works import ImageDetach, WorkChange, WorkRef
from komaquill.domain.models import Run


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


def test_image_output_requires_detachment_and_preserves_other_indices(tmp_path):
    api = Client.initialize(tmp_path)
    run = Run(
        run_id=uuid4(),
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
    api._project.repository.save(run)
    character = api.library.store.create(
        "character", CharacterInput(name="Character"), image_id=run.run_id
    )
    story = api.stories.create(StoryInput(name="Story", character_id=character.id))
    target = WorkRef(kind="image", id=run.run_id)
    trash = WorkChange(target=target, action="trash", operation_id=uuid4(), expected_revision=0)
    with pytest.raises(KomaquillError, match="image_in_use"):
        api.works.change(trash)
    (use,) = api.works.image_uses(target)
    api.works.detach(ImageDetach(image=target, use=use))
    assert api.characters.get(character.id).image_id is None
    assert api.stories.get(story.id).character_id == character.id
    api.works.change(trash)
    assert [item.index for item in api.works.image_outputs([run])[run.run_id]] == [1]
    assert api.images.get(run.run_id).images == ["cover.png", "candidate.png"]
