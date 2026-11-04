"""Verify save receipts, immutable render input and image completion without a browser."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from komaquill.adapters.library_repository import creation_jobs
from komaquill.api import CharacterInput, Client, KomaquillError, PresetSaveOptions, StoryInput
from sqlalchemy import select


def prepared(tmp_path):
    api = Client.initialize(tmp_path)
    calls, runs = [], {}

    def get(identity):
        if identity not in runs:
            raise KomaquillError(code="request_not_found")
        return runs[identity]

    def submit(prompt, *, run_id, parameters, purpose=None):
        calls.append((prompt, run_id, parameters))
        runs[run_id] = SimpleNamespace(run_id=run_id, state="queued", images=[])
        return runs[run_id]

    sources = []

    def translate(source, *args, **kwargs):
        sources.append(json.loads(source))
        return "A sixty-year-old man with short silver hair, wearing a black apron."

    api.library.images = SimpleNamespace(get=get, submit=submit)
    api.library.image_lookup = api.library.images.get
    api.library.image_describer = SimpleNamespace(prepare_image=translate)
    api.library.preflight = lambda *_: None
    return api, calls, runs, sources


def options(**changes):
    return PresetSaveOptions(operation_id=uuid4(), **changes)


def draw(api, preset, kind="character"):
    from komaquill.api import CreationRequest

    request = CreationRequest(
        operation_id=uuid4(),
        kind="portrait" if kind == "character" else "banner",
        target_id=preset.id,
        expected_revision=preset.revision,
        llm_profile="fake",
    )
    job, created = api.library.accept(request)
    assert created
    return api.library.execute(job.id)


def test_save_is_local_idempotent_and_does_not_generate(tmp_path):
    api, calls, _, sources = prepared(tmp_path)
    request = options()
    value = CharacterInput(name="Mira", age="60", gender="man", initial_clothing="Black apron")
    saved = api.library.save_preset("character", value, request)
    assert calls == sources == []
    assert saved.preset.image_id is None and saved.preset.image_job_id is None
    assert api.library.save_preset("character", value, request) == saved
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.library.save_preset("character", value.model_copy(update={"age": "61"}), request)
    job = draw(api, saved.preset)
    assert job.state == "submitted" and len(calls) == 1
    assert sources[0]["age"] == "60" and sources[0]["gender"] == "man"


def test_visual_save_keeps_old_image_and_marks_it_stale_until_explicit_generation(tmp_path):
    api, calls, runs, _ = prepared(tmp_path)
    value = CharacterInput(name="Mira", age="60")
    first = api.library.save_preset("character", value, options()).preset
    job = draw(api, first)
    runs[job.id].state, runs[job.id].images = "saved", ["old.png"]
    api.library.reconcile()
    current = api.characters.get(first.id)
    changed = api.library.save_preset(
        "character",
        value.model_copy(update={"age": "61"}),
        options(target_id=current.id, expected_revision=current.revision),
    ).preset
    assert changed.image_id == job.id and changed.image_stale
    assert changed.image_job_id is None and len(calls) == 1
    duplicated = api.characters.duplicate(changed.id)
    assert duplicated.image_id == job.id and duplicated.image_stale
    assert duplicated.age == changed.age and duplicated.image_job_id is None
    new_job = draw(api, changed)
    runs[new_job.id].state, runs[new_job.id].images = "saved", ["new.png"]
    api.library.reconcile()
    result = api.characters.get(first.id)
    assert result.image_id == new_job.id and not result.image_stale


def test_concurrent_same_id_is_one_local_save(tmp_path):
    api, calls, _, _ = prepared(tmp_path)
    request = options()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: api.library.save_preset(
                    "character", CharacterInput(name="Once"), request
                ),
                range(2),
            )
        )
    assert results[0] == results[1] and calls == []
    assert len([p for p in api.characters.list() if p.name == "Once"]) == 1
    with api.library.store.database.engine.connect() as connection:
        assert connection.execute(select(creation_jobs.c.id)).first() is None


def test_stale_image_completion_does_not_attach_to_changed_or_deleted_preset(tmp_path):
    api, _, runs, _ = prepared(tmp_path)
    value = CharacterInput(name="Mira", initial_clothing="Apron")
    preset = api.library.save_preset("character", value, options()).preset
    old_job = draw(api, preset)
    changed = api.library.save_preset(
        "character",
        value.model_copy(update={"initial_clothing": "Shirt"}),
        options(target_id=preset.id, expected_revision=0),
    ).preset
    runs[old_job.id].state, runs[old_job.id].images = "saved", ["old.png"]
    api.library.reconcile()
    assert api.characters.get(preset.id).image_id is None
    new_job = draw(api, changed)
    api.characters.delete(changed.id, changed.revision)
    runs[new_job.id].state, runs[new_job.id].images = "saved", ["new.png"]
    assert api.library.reconcile() == []
    assert api.library.job(new_job.id).state == "complete"


def test_nonvisual_save_keeps_pending_image_reference(tmp_path):
    api, calls, runs, _ = prepared(tmp_path)
    value = CharacterInput(name="Mira", personality="Quiet")
    first = api.library.save_preset("character", value, options()).preset
    job = draw(api, first)
    changed = api.library.save_preset(
        "character",
        value.model_copy(update={"personality": "Cheerful"}),
        options(target_id=first.id, expected_revision=0),
    ).preset
    assert changed.image_job_id == job.id and not changed.image_stale
    runs[job.id].state, runs[job.id].images = "saved", ["image.png"]
    api.library.reconcile()
    assert api.characters.get(first.id).image_id == job.id and len(calls) == 1


def test_corrupt_job_is_isolated_and_diagnostic_records_only_changes(tmp_path, monkeypatch):
    from komaquill.adapters import diagnostics
    from komaquill.adapters.creation_reconciliation import reconcile_creations

    api, _, runs, _ = prepared(tmp_path)
    bad = draw(api, api.characters.create(CharacterInput(name="Bad")))
    good = draw(api, api.stories.create(StoryInput(name="Good", world="Coast")), "story")
    with api.library.store.database.engine.begin() as connection:
        original = connection.execute(
            select(creation_jobs.c.payload).where(creation_jobs.c.id == str(bad.id))
        ).scalar_one()
        connection.execute(
            creation_jobs.update()
            .where(creation_jobs.c.id == str(bad.id))
            .values(payload="invalid")
        )
    runs[good.id].state, runs[good.id].images = "saved", ["good.png"]
    api.library.reconcile()
    events = []
    monkeypatch.setattr(
        diagnostics, "record_event", lambda *args, **kwargs: events.append((args, kwargs)) or True
    )
    reconcile_creations(tmp_path)
    reconcile_creations(tmp_path)
    assert len(events) == 1 and events[0][0][2] == "failed"
    assert api.stories.get(good.request.target_id).image_id == good.id
    with api.library.store.database.engine.begin() as connection:
        connection.execute(
            creation_jobs.update().where(creation_jobs.c.id == str(bad.id)).values(payload=original)
        )
    reconcile_creations(tmp_path)
    reconcile_creations(tmp_path)
    assert len(events) == 2 and events[1][0][2] == "restored"


def test_invalid_save_references_fail_without_generation(tmp_path):
    api, calls, _, _ = prepared(tmp_path)
    for request, code in [
        (options(target_id=uuid4(), expected_revision=0), "request_not_found"),
        (options(expected_revision=1), "operation_conflict"),
    ]:
        with pytest.raises(KomaquillError, match=code):
            api.library.save_preset("character", CharacterInput(name="Missing"), request)
    assert calls == []
