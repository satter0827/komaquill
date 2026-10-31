"""Exercise saved creators, frozen play settings and idempotent media work."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from komaquill.adapters.library_repository import creation_jobs, presets
from komaquill.api import (
    CharacterInput,
    Client,
    ConversationStart,
    CreationRequest,
    KomaquillError,
    PlayerInput,
    PresetRef,
    StoryInput,
)
from komaquill.conversations.models import SendInput
from komaquill.interfaces.http.app import create_app
from komaquill.library.service import sample_id


def selection(locale="ja", profile="fake"):
    return ConversationStart(
        **{
            kind: PresetRef(id=sample_id(locale, kind), revision=0)
            for kind in ("character", "story")
        },
        player_revision=0,
        locale=locale,
        llm_profile=profile,
    )


@pytest.mark.parametrize("model", [PlayerInput, CharacterInput, StoryInput])
def test_presets_require_a_visible_name(model):
    with pytest.raises(ValueError):
        model(name=" \t ")


@pytest.mark.parametrize(
    "kind,model",
    [("characters", CharacterInput), ("stories", StoryInput)],
)
def test_crud_is_typed_persistent_and_revision_checked(tmp_path, kind, model):
    api = Client.initialize(tmp_path)
    service = getattr(api, kind)
    assert len(service.list()) == 2
    created = service.create(model(name="Original", description="Retained"))
    assert not created.sample
    assert getattr(Client(tmp_path), kind).get(created.id) == created
    changed = service.update(created.id, 0, model(name="Changed"))
    assert changed.revision == 1 and changed.sample_locale is None
    for action in (
        lambda: service.update(created.id, 0, model(name="Stale")),
        lambda: service.delete(created.id, 0),
    ):
        with pytest.raises(KomaquillError, match="operation_conflict"):
            action()
    duplicate = service.duplicate(created.id)
    assert duplicate.id != created.id and duplicate.name == "Changed" and duplicate.revision == 0
    service.delete(created.id, 1)
    with pytest.raises(KomaquillError, match="request_not_found"):
        service.get(created.id)
    assert duplicate in service.list()


@pytest.mark.parametrize("locale,greeting", [("ja", "こんにちは"), ("en", "Hello")])
def test_play_uses_locale_samples_and_keeps_deleted_settings(tmp_path, locale, greeting):
    api = Client.initialize(tmp_path)
    library = api.library
    conversation = library.start(selection(locale))
    character = api.characters.get(sample_id(locale, "character"))
    api.characters.update(character.id, 0, CharacterInput(name="Changed"))
    api.characters.delete(character.id, 1)
    request = SendInput(operation_id=uuid4(), text=greeting)
    assert api.conversations.send(conversation.id, request).state == "complete"
    reopened = Client(tmp_path).conversations.get(conversation.id)
    assert character.name in reopened.definition.character and reopened.definition.sample
    assert reopened.definition.presets["character"].name == character.name
    assert len(reopened.turns) == 1
    with pytest.raises(KomaquillError, match="request_not_found"):
        library.start(selection(locale))


def test_custom_settings_cannot_impersonate_sample_and_references_are_checked(tmp_path):
    api = Client.initialize(tmp_path)
    library = api.library
    character = api.characters.create(CharacterInput(name="Custom"))
    request = selection().model_copy(update={"character": PresetRef(id=character.id, revision=0)})
    with pytest.raises(KomaquillError, match="fake_input_mismatch"):
        library.start(request)
    request = request.model_copy(update={"llm_profile": "lmstudio-4b"})
    saved = library.start(request)
    assert not saved.definition.sample and "Custom" in saved.definition.character
    api.characters.update(character.id, 0, CharacterInput(name="New"))
    with pytest.raises(KomaquillError, match="operation_conflict"):
        library.start(request)
    with pytest.raises(KomaquillError, match="request_not_found"):
        api.stories.create(StoryInput(name="Missing", character_id=uuid4()))


def test_preferences_are_checked_and_frozen_in_new_play(tmp_path):
    api = Client.initialize(tmp_path)
    initial = api.library.preferences()
    assert initial.locale == "ja"
    saved = api.library.save_preferences(
        initial.model_copy(update={"locale": "en", "response_length": "short"})
    )
    assert saved.revision == 1 and Client(tmp_path).library.preferences() == saved
    assert api.library.start(selection("en")).definition.response_length == "short"
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.library.save_preferences(initial)
    for changes, code in [
        ({"llm_profile": "missing"}, "invalid_configuration"),
    ]:
        with pytest.raises(KomaquillError, match=code):
            api.library.save_preferences(saved.model_copy(update=changes))


def test_display_preferences_can_be_saved_when_generation_configuration_is_invalid(tmp_path):
    api = Client.initialize(tmp_path)
    library = api.library
    (tmp_path / "komaquill.toml").write_text(
        '[conversation]\nrequest_timeout="invalid"\n', encoding="utf-8"
    )
    saved = library.save_preferences(
        library.preferences().model_copy(update={"locale": "en", "font_size": "large"})
    )
    assert Client(tmp_path).library.preferences() == saved
    with pytest.raises(KomaquillError, match="invalid_configuration"):
        library.save_preferences(saved.model_copy(update={"llm_profile": "other"}))


@pytest.mark.parametrize("field", ["id", "operation_id", "image_id"])
def test_corrupt_creation_identity_stops_before_inference_and_listing(tmp_path, field):
    import json

    api = Client.initialize(tmp_path)
    library = api.library
    conversation = library.start(selection())
    request = CreationRequest(
        operation_id=uuid4(),
        kind="conversation_image",
        target_id=conversation.id,
        expected_revision=0,
    )
    job, _ = library.accept(request)
    value = job.model_dump(mode="json")
    if field == "operation_id":
        value["request"][field] = str(uuid4())
    else:
        value[field] = str(uuid4())
    with library.store.database.engine.begin() as connection:
        connection.execute(creation_jobs.update().values(payload=json.dumps(value)))
    calls = []
    library.creator = SimpleNamespace(prepare_image=lambda *_a, **_k: calls.append(True))
    for action in (
        lambda: library.execute(job.id),
        lambda: library.store.job(job.id),
        lambda: library.conversation_images(conversation.id),
        lambda: library.store.accept(request, "changed"),
    ):
        with pytest.raises(KomaquillError, match="storage_failed"):
            action()
    assert calls == [] and api.images.list() == []


@pytest.mark.parametrize("field", ["turn_id", "created_at", "prompt_en"])
def test_creation_checkpoints_preserve_the_scene_and_applied_prompt(tmp_path, field):
    library = Client.initialize(tmp_path).library
    job, _ = library.accept(
        CreationRequest(operation_id=uuid4(), kind="story_draft", prompt="Idea")
    )
    job = library.store.save_job(job.model_copy(update={"prompt_en": "Frozen English prompt"}))
    change = uuid4() if field == "turn_id" else "Changed"
    with pytest.raises(KomaquillError, match="operation_conflict"):
        library.store.save_job(job.model_copy(update={field: change}))
    assert library.store.job(job.id)[0] == job


def test_image_execution_reuses_saved_english_without_retranslation(tmp_path):
    library = Client.initialize(tmp_path).library
    character = library.characters.create(CharacterInput(name="Portrait"))
    job, _ = library.accept(
        CreationRequest(
            operation_id=uuid4(),
            kind="portrait",
            target_id=character.id,
            expected_revision=0,
        )
    )
    library.store.save_job(job.model_copy(update={"prompt_en": "Frozen English prompt"}))
    translations, submissions = [], []
    library.creator = SimpleNamespace(prepare_image=lambda *_a, **_k: translations.append(True))
    library.images = SimpleNamespace(
        submit=lambda prompt, **_k: submissions.append(prompt),
        get=lambda identity: SimpleNamespace(state="queued", images=[], run_id=identity),
    )
    assert library.execute(job.id).state == "submitted"
    assert translations == [] and submissions == ["Frozen English prompt"]


def test_deleting_default_selections_does_not_block_preferences_or_story_edits(tmp_path):
    api = Client.initialize(tmp_path)
    character = api.characters.create(CharacterInput(name="Default"))
    story = api.stories.create(StoryInput(name="Story", character_id=character.id))
    api.characters.delete(character.id, 0)
    updated = api.stories.get(story.id)
    assert updated.character_id is None and updated.revision == 1
    assert (
        api.stories.update(updated.id, updated.revision, StoryInput(name="Edited")).name == "Edited"
    )


def test_duplicate_retains_an_existing_image_reference_without_generating_again(tmp_path):
    api = Client.initialize(tmp_path)
    character = api.characters.create(CharacterInput(name="Portrait"))
    image = uuid4()
    api.library.store.attach("character", character.id, 0, image)
    api.library.store.attach("character", character.id, 0, image, image)
    duplicated = api.characters.duplicate(character.id)
    assert duplicated.image_id == image and duplicated.image_job_id is None


@pytest.mark.parametrize("case", ["custom_text", "forged_sample", "wrong_locale", "changed_state"])
def test_fixed_samples_do_not_claim_to_understand_arbitrary_settings(tmp_path, case):
    api = Client.initialize(tmp_path)
    if case == "custom_text":
        from komaquill.conversations.models import ConversationInput

        conversation = api.conversations.create(
            ConversationInput(
                locale="en", llm_profile="fake", title="Custom", character="A pilot named Nova"
            )
        )
    else:
        conversation = api.library.start(selection())
    if case == "forged_sample":
        conversation = api.conversations.create(
            conversation.definition.model_copy(update={"character": "Arbitrary settings"})
        )
    elif case == "changed_state":
        from komaquill.conversations.models import StateCommand

        api.conversations.command(
            conversation.id,
            StateCommand(command="set_state", expected_revision=0, state={"place": "Elsewhere"}),
        )
    result = api.conversations.send(
        conversation.id,
        SendInput(
            operation_id=uuid4(), text="Hello", locale="en" if case == "wrong_locale" else None
        ),
    )
    assert (
        result.error == "fake_input_mismatch" and api.conversations.get(conversation.id).turns == []
    )


@pytest.mark.parametrize("kind", ["character_draft", "story_draft"])
def test_draft_is_saved_once_and_recovery_does_not_resend(tmp_path, kind):
    api = Client.initialize(tmp_path)
    library = api.library
    calls = []
    result = CharacterInput(name="Draft") if kind == "character_draft" else StoryInput(name="Draft")
    library.creator = SimpleNamespace(draft=lambda value: calls.append(value) or result)
    request = CreationRequest(operation_id=uuid4(), kind=kind, prompt="Idea")
    accepted, created = library.accept(request)
    assert created and accepted.state == "accepted"
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(library.execute, [accepted.id, accepted.id]))
    assert len(calls) == 1 and library.job(accepted.id).result == result
    assert library.accept(request)[1] is False
    assert Client(tmp_path).library.job(accepted.id).result == result
    with pytest.raises(KomaquillError, match="operation_conflict"):
        library.accept(request.model_copy(update={"prompt": "Different"}))
    # A stale observer must not roll a terminal checkpoint backwards.
    assert library.store.save_job(accepted).state == "complete"


@pytest.mark.parametrize("kind", ["portrait", "banner"])
@pytest.mark.parametrize("change", ["none", "edit", "delete", "newer_job"])
def test_images_attach_only_to_the_accepted_version(tmp_path, kind, change):
    api = Client.initialize(tmp_path)
    library = api.library
    service = api.characters if kind == "portrait" else api.stories
    model = CharacterInput if kind == "portrait" else StoryInput
    value = service.create(model(name="Saved", description="Original setting"))
    calls = []
    runs = {}

    def submit(prompt, *, run_id, parameters):
        calls.append((prompt, parameters))
        runs[run_id] = SimpleNamespace(run_id=run_id, state="queued", images=[])
        return runs[run_id]

    def get(identity):
        if identity not in runs:
            raise KomaquillError(code="request_not_found")
        return runs[identity]

    library.images = SimpleNamespace(submit=submit, get=get)
    library.creator = SimpleNamespace(
        prepare_image=lambda *args, **kwargs: (
            "One person in a bookshop" if kind == "portrait" else "Wide view of a bookshop, no text"
        )
    )
    request = CreationRequest(
        operation_id=uuid4(), kind=kind, target_id=value.id, expected_revision=0
    )
    job, _ = library.accept(request)
    assert library.execute(job.id).state == "submitted"
    assert library.execute(job.id).state == "submitted" and len(calls) == 1
    assert calls[0][1]["width"] != calls[0][1]["height"]
    if change == "edit":
        service.update(value.id, 0, model(name="Edited"))
    elif change == "delete":
        service.delete(value.id, 0)
    elif change == "newer_job":
        library.accept(request.model_copy(update={"operation_id": uuid4()}))
    runs[job.id].state = "saved"
    runs[job.id].images = ["image.png"]
    assert library.job(job.id).state == "complete"
    if change != "delete":
        assert service.get(value.id).image_id == (job.id if change == "none" else None)


@pytest.mark.parametrize("failure", ["not_allowed", "unexpected", "dead", "image_failed"])
def test_failure_preserves_presets_and_does_not_retry(tmp_path, failure):
    api = Client.initialize(tmp_path)
    library = api.library
    value = api.characters.create(CharacterInput(name="Kept"))
    request = CreationRequest(
        operation_id=uuid4(), kind="portrait", target_id=value.id, expected_revision=0
    )
    job, _ = library.accept(request)
    if failure == "dead":
        with library.store.database.engine.begin() as connection:
            connection.execute(creation_jobs.update().values(owner_started="0"))
        assert library.execute(job.id).state == "interrupted"
    elif failure == "image_failed":
        library.images = SimpleNamespace(get=lambda _: SimpleNamespace(state="failed", images=[]))
        assert library.job(job.id).state == "failed"
    else:
        if failure == "unexpected":
            library.creator = SimpleNamespace(prepare_image=lambda *_, **kwargs: 1 / 0)
        assert library.execute(job.id).state == "failed"
    assert api.characters.get(value.id).name == "Kept"
    assert library.accept(request)[1] is False


@pytest.mark.parametrize("kind", ["portrait", "banner", "conversation_image"])
@pytest.mark.parametrize("outcome", ["saved", "failed"])
def test_unknown_image_submission_recovers_after_restart_without_resending(
    project, client, session, stub, monkeypatch, kind, outcome
):
    from contextlib import contextmanager

    @contextmanager
    def connected(_workspace):
        yield client, session

    monkeypatch.setattr("komaquill.api.images.connected", connected)
    api = Client.initialize(project.root)
    translations = []
    api.library.creator = SimpleNamespace(
        prepare_image=lambda *_a, **_kw: translations.append(True) or "A quiet bookshop."
    )
    if kind == "conversation_image":
        target = api.library.start(selection())
    else:
        service = api.characters if kind == "portrait" else api.stories
        model = CharacterInput if kind == "portrait" else StoryInput
        target = service.create(model(name="Retained"))
    request = CreationRequest(
        operation_id=uuid4(),
        kind=kind,
        target_id=target.id,
        expected_revision=0,
    )
    accepted, _ = api.library.accept(request)
    stub.drop_response = True
    submitted = api.library.execute(accepted.id)
    assert submitted.state == "submitted" and submitted.image_id == accepted.id
    assert submitted.error == "submission_unknown"
    assert api.images.get(accepted.id).state == "unknown"
    with api.library.store.database.engine.begin() as connection:
        connection.execute(creation_jobs.update().values(owner_started="0"))
    reopened = Client(project.root)
    assert reopened.library.job(accepted.id) == submitted
    assert reopened.library.accept(request) == (submitted, False)
    if outcome == "failed":
        next(iter(stub.history.values()))["status"] = {"status_str": "error", "completed": False}
    reopened.images.collect(accepted.id)
    final = reopened.library.job(accepted.id)
    assert final.state == ("complete" if outcome == "saved" else "failed")
    assert final.error == (None if outcome == "saved" else "generation_failed")
    assert final.prompt_en == submitted.prompt_en and final.original == accepted.original
    assert reopened.library.execute(accepted.id) == final
    if kind == "conversation_image":
        assert reopened.library.conversation_images(target.id) == [final]
    else:
        service = reopened.characters if kind == "portrait" else reopened.stories
        assert service.get(target.id).image_id == (accepted.id if outcome == "saved" else None)
    assert translations == [True] and stub.submissions == 1


def test_invalid_media_and_corrupt_records_fail_safely(tmp_path):
    api = Client.initialize(tmp_path)
    library = api.library
    for request in [
        CreationRequest(operation_id=uuid4(), kind="portrait"),
        CreationRequest(operation_id=uuid4(), kind="story_draft"),
    ]:
        with pytest.raises(KomaquillError, match="invalid_request"):
            library.accept(request)
    value = api.characters.create(CharacterInput(name="Kept"))
    with pytest.raises(KomaquillError, match="operation_conflict"):
        library.accept(
            CreationRequest(
                operation_id=uuid4(), kind="portrait", target_id=value.id, expected_revision=5
            )
        )
    with library.store.database.engine.begin() as connection:
        connection.execute(
            presets.update()
            .where(presets.c.id == str(value.id))
            .values(payload=value.model_copy(update={"id": uuid4()}).model_dump_json())
        )
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.characters.get(value.id)


@pytest.mark.parametrize("collection", ["characters", "stories"])
def test_http_crud_and_start_share_the_python_services(tmp_path, collection):
    api = Client.initialize(tmp_path)
    with TestClient(create_app(api)) as http:
        assert http.get("/api/v1/preferences").json()["locale"] == "ja"
        prefs = api.library.preferences().model_dump(mode="json")
        assert http.put("/api/v1/preferences", json={**prefs, "locale": "en"}).status_code == 200
        base = f"/api/v1/{collection}"
        assert len(http.get(base).json()) == 2
        value = http.post(base, json={"name": "Created"}).json()
        url = base + "/" + value["id"]
        assert http.get(url).json()["name"] == "Created"
        assert http.put(url + "?expected_revision=0", json={"name": "Updated"}).status_code == 200
        assert http.post(url + "/duplicate").status_code == 201
        assert http.delete(url + "?expected_revision=1").status_code == 204
        assert http.get(url).status_code == 404
        assert (
            http.post(
                "/api/v1/conversations/from-presets", json=selection("en").model_dump(mode="json")
            ).status_code
            == 201
        )
        request = CreationRequest(operation_id=uuid4(), kind="story_draft", prompt="Idea")
        assert (
            http.post("/api/v1/creation-jobs", json=request.model_dump(mode="json")).status_code
            == 202
        )
        assert http.get(f"/api/v1/creation-jobs/{request.operation_id}").json()["state"] == "failed"
