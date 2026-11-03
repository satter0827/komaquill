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

    api.library.images = SimpleNamespace(available=lambda: True, get=get, submit=submit)
    api.library.image_lookup = api.library.images.get
    api.library.image_describer = SimpleNamespace(prepare_image=translate)
    return api, calls, runs, sources


def options(**changes):
    return PresetSaveOptions(operation_id=uuid4(), allow_real_llm=True, **changes)


def test_save_and_regenerate_use_current_fields_and_recover_the_same_receipt(tmp_path):
    api, calls, runs, sources = prepared(tmp_path)
    request = options()
    value = CharacterInput(
        name="Mira", age="60", gender="man", appearance="Short silver hair, black apron"
    )
    saved = api.library.save_preset("character", value, request)
    assert sources[0]["age"] == "60" and sources[0]["gender"] == "man"
    assert saved.preset.image_job_id == request.operation_id and saved.job.state == "submitted"
    assert api.library.save_preset("character", value, request).preset == saved.preset
    assert len(calls) == 1
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.library.save_preset("character", value.model_copy(update={"age": "61"}), request)
    runs[saved.job.id].state = "saved"
    runs[saved.job.id].images = ["image.png"]
    assert api.library.reconcile() == []
    assert api.characters.get(saved.preset.id).image_id == saved.job.id
    changed = api.library.save_preset(
        "character",
        value.model_copy(update={"age": "61"}),
        options(target_id=saved.preset.id, expected_revision=0),
    )
    assert changed.preset.revision == 1 and changed.preset.image_id == saved.job.id
    assert sources[-1]["age"] == "61" and len(calls) == 2


@pytest.mark.parametrize("failure", ["unavailable", "permission", "connection"])
def test_media_failure_preserves_text_and_does_not_start_generation(tmp_path, failure):
    api, calls, _, _ = prepared(tmp_path)
    if failure == "unavailable":
        api.library.images.available = lambda: False
    if failure == "connection":
        api.library.images.available = lambda: (_ for _ in ()).throw(
            KomaquillError(code="connection_failed")
        )
    request = options().model_copy(update={"allow_real_llm": failure != "permission"})
    result = api.library.save_preset("character", CharacterInput(name="Retained"), request)
    assert result.image_error and result.job is None and calls == []
    assert Client(tmp_path).characters.get(result.preset.id).name == "Retained"
    api.library.images.available = lambda: True
    assert api.library.save_preset("character", CharacterInput(name="Retained"), request) == result


def test_nonvisual_edit_does_not_generate_and_explicit_regeneration_does(tmp_path):
    api, calls, runs, _ = prepared(tmp_path)
    value = CharacterInput(name="Mira", appearance="Short hair", personality="Quiet")
    first = api.library.save_preset("character", value, options())
    runs[first.job.id].state, runs[first.job.id].images = "saved", ["image.png"]
    api.library.reconcile()
    changed = value.model_copy(update={"personality": "Cheerful"})
    second = api.library.save_preset(
        "character", changed, options(target_id=first.preset.id, expected_revision=0)
    )
    assert second.job is None and second.preset.revision == 1 and len(calls) == 1
    third = api.library.save_preset(
        "character",
        changed,
        options(target_id=first.preset.id, expected_revision=1, image_policy="always"),
    )
    assert third.job is not None and third.preset.revision == 1 and len(calls) == 2


def test_concurrent_same_id_is_one_save_and_one_accepted_job(tmp_path):
    api, calls, _, _ = prepared(tmp_path)
    request = options()
    value = CharacterInput(name="Once", age="60")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: api.library.save_preset("character", value, request, execute=False),
                range(2),
            )
        )
    assert results[0] == results[1] and calls == []
    assert len([item for item in api.characters.list() if item.name == "Once"]) == 1
    api.library.execute(request.operation_id)
    assert len(calls) == 1


def test_different_id_busy_does_not_steal_reference_and_stale_results_do_not_apply(tmp_path):
    api, calls, runs, _ = prepared(tmp_path)
    value = CharacterInput(name="Mira", appearance="Black apron")
    first = api.library.save_preset("character", value, options())
    second = api.library.save_preset(
        "character",
        value,
        options(target_id=first.preset.id, expected_revision=0, image_policy="always"),
    )
    assert second.image_error == "operation_busy" and second.job is None
    assert second.preset.image_job_id == first.job.id and len(calls) == 1
    changed = api.library.save_preset(
        "character",
        value.model_copy(update={"appearance": "Blue shirt"}),
        options(target_id=first.preset.id, expected_revision=0),
    )
    runs[first.job.id].state, runs[first.job.id].images = "saved", ["old.png"]
    api.library.reconcile()
    current = api.characters.get(first.preset.id)
    assert current.image_id is None and current.image_job_id == changed.job.id
    api.characters.delete(current.id, current.revision)
    runs[changed.job.id].state, runs[changed.job.id].images = "saved", ["new.png"]
    assert api.library.reconcile() == []
    assert api.library.job(changed.job.id).state == "complete"


def test_corrupt_job_is_reported_without_blocking_healthy_completion(tmp_path):
    api, _, runs, _ = prepared(tmp_path)
    bad = api.library.save_preset("character", CharacterInput(name="Bad"), options())
    good = api.library.save_preset(
        "story", StoryInput(name="Good", world="A quiet beach"), options()
    )
    with api.library.store.database.engine.begin() as connection:
        connection.execute(
            creation_jobs.update()
            .where(creation_jobs.c.id == str(bad.job.id))
            .values(payload="invalid")
        )
    runs[good.job.id].state, runs[good.job.id].images = "saved", ["image.png"]
    assert api.library.reconcile() == [bad.job.id]
    assert api.stories.get(good.preset.id).image_id == good.job.id


def test_invalid_save_references_fail_before_any_generation(tmp_path):
    api, calls, _, _ = prepared(tmp_path)
    for kind, value, request, code in [
        ("player", CharacterInput(name="No"), options(), "invalid_request"),
        (
            "character",
            CharacterInput(name="Missing"),
            options(target_id=uuid4(), expected_revision=0),
            "request_not_found",
        ),
        (
            "character",
            CharacterInput(name="Revision"),
            options(expected_revision=1),
            "operation_conflict",
        ),
        (
            "story",
            StoryInput(name="Missing partner", character_id=uuid4()),
            options(),
            "request_not_found",
        ),
    ]:
        with pytest.raises(KomaquillError, match=code):
            api.library.save_preset(kind, value, request)
    assert calls == []
    with api.library.store.database.engine.connect() as connection:
        assert connection.execute(select(creation_jobs.c.id)).first() is None


def test_nonvisual_edit_while_pending_keeps_the_same_render_and_can_finish(tmp_path):
    api, calls, runs, _ = prepared(tmp_path)
    value = CharacterInput(name="Mira", appearance="Black apron", personality="Quiet")
    first = api.library.save_preset("character", value, options())
    second = api.library.save_preset(
        "character",
        value.model_copy(update={"personality": "Cheerful"}),
        options(target_id=first.preset.id, expected_revision=0),
    )
    assert second.preset.revision == 1 and second.preset.image_job_id == first.job.id
    assert second.job is None and len(calls) == 1
    runs[first.job.id].state, runs[first.job.id].images = "saved", ["image.png"]
    api.library.reconcile()
    assert api.characters.get(first.preset.id).image_id == first.job.id


def test_unreadable_other_job_does_not_block_a_new_target(tmp_path):
    api, calls, _, _ = prepared(tmp_path)
    first = api.library.save_preset("character", CharacterInput(name="Bad"), options())
    with api.library.store.database.engine.begin() as connection:
        connection.execute(
            creation_jobs.update()
            .where(creation_jobs.c.id == str(first.job.id))
            .values(payload="invalid")
        )
    second = api.library.save_preset("character", CharacterInput(name="Good"), options())
    assert second.job.state == "submitted" and len(calls) == 2
    assert first.job.id in api.library.reconcile()


def test_removing_default_partner_does_not_discard_pending_banner(tmp_path):
    api, _, runs, _ = prepared(tmp_path)
    partner = api.characters.create(CharacterInput(name="Mira"))
    saved = api.library.save_preset(
        "story", StoryInput(name="Beach", world="Coast", character_id=partner.id), options()
    )
    api.characters.delete(partner.id, partner.revision)
    current = api.stories.get(saved.preset.id)
    assert current.character_id is None and current.image_job_id == saved.job.id
    runs[saved.job.id].state, runs[saved.job.id].images = "saved", ["image.png"]
    api.library.reconcile()
    assert api.stories.get(current.id).image_id == saved.job.id


def test_corrupt_unrelated_job_does_not_break_conversation_image_listing(tmp_path):
    api, _, _, _ = prepared(tmp_path)
    saved = api.library.save_preset("character", CharacterInput(name="Bad"), options())
    with api.library.store.database.engine.begin() as connection:
        connection.execute(
            creation_jobs.update()
            .where(creation_jobs.c.id == str(saved.job.id))
            .values(payload="invalid")
        )
    assert api.library.store.conversation_images(uuid4()) == []
    assert saved.job.id in api.library.reconcile()


def test_monitor_reconciliation_tolerates_an_accepted_job_before_image_record_exists(tmp_path):
    from komaquill.adapters.creation_reconciliation import reconcile_creations

    api = Client.initialize(tmp_path)
    preset = api.characters.create(CharacterInput(name="Pending"))
    from komaquill.api import CreationRequest

    request = CreationRequest(
        operation_id=uuid4(),
        kind="portrait",
        target_id=preset.id,
        expected_revision=0,
        allow_real_llm=True,
    )
    api.library.accept(request)
    reconcile_creations(tmp_path)
    assert api.library.job(request.operation_id).state == "accepted"
    assert api.characters.get(preset.id).image_job_id == request.operation_id
    with api.library.store.database.engine.begin() as connection:
        connection.execute(
            creation_jobs.update()
            .where(creation_jobs.c.id == str(request.operation_id))
            .values(payload="invalid")
        )
    reconcile_creations(tmp_path)


def test_reconciliation_reports_only_failure_changes_and_recovery(tmp_path, monkeypatch):
    from komaquill.adapters import diagnostics
    from komaquill.adapters.creation_reconciliation import reconcile_creations

    api, _, _, _ = prepared(tmp_path)
    saved = api.library.save_preset("character", CharacterInput(name="Bad"), options())
    with api.library.store.database.engine.begin() as connection:
        original = connection.execute(
            select(creation_jobs.c.payload).where(creation_jobs.c.id == str(saved.job.id))
        ).scalar_one()
        connection.execute(creation_jobs.update().values(payload="invalid"))
    events = []
    monkeypatch.setattr(
        diagnostics, "record_event", lambda *args, **kwargs: events.append((args, kwargs)) or True
    )
    reconcile_creations(tmp_path)
    reconcile_creations(tmp_path)
    assert len(events) == 1 and events[0][0][2] == "failed"
    with api.library.store.database.engine.begin() as connection:
        connection.execute(creation_jobs.update().values(payload=original))
    reconcile_creations(tmp_path)
    reconcile_creations(tmp_path)
    assert len(events) == 2 and events[1][0][2] == "restored"
