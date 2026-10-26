"""Verify standard batching, frozen conditions and partial candidate collection."""

import json
from copy import deepcopy

import pytest

from komaquill.application.generation import collect, generation_settings, submit_generation
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import compile_image_input, preset_fingerprint
from komaquill.domain.presets import GenerationPreset
from komaquill.domain.workflow import digest
from komaquill.infrastructure.configuration import Settings


@pytest.mark.parametrize("count", [1, 2, 4])
def test_candidate_batch_is_one_request_with_portable_output_metadata(
    project, session, client, stub, count
):
    active = session.model_copy(update={"settings": project.settings()})
    run = submit_generation(
        project, client, active, "a blue cabin", "extra windows", 42, candidates=count
    )
    assert stub.submissions == 1 and run.output_spec.candidates == count
    assert collect(project, client, active, run.run_id).saved == 1
    saved = project.repository.read(run.run_id)
    assert len(saved.images) == count
    assert [saved.image_metadata[name].batch_index for name in saved.images] == list(range(count))
    assert all(
        meta.role == "candidate" and meta.output_node == "9"
        for meta in saved.image_metadata.values()
    )
    graph = json.loads(project.artifacts_store.read(f"runs/{run.run_id}/workflow-api.json"))
    assert graph["5"]["inputs"]["batch_size"] == count
    assert active.settings.presets["pony"].batch_size == 1


@pytest.mark.parametrize("count", [0, -1, 5, True, 1.5, "2"])
def test_invalid_candidate_count_does_not_submit_or_record(project, session, client, stub, count):
    with pytest.raises(KomaquillError) as error:
        submit_generation(project, client, session, "a cabin", None, 42, candidates=count)
    assert error.value.code == "invalid_request" and stub.submissions == 0
    assert project.repository.load() == []


def test_bundled_defaults_and_explicit_override_are_independent():
    settings = Settings()
    assert settings.presets["pony"].batch_size == 2
    assert settings.story_max_in_flight == 2
    changed = generation_settings(settings, "pony", 4)
    assert changed.presets["pony"].batch_size == 4
    assert settings.presets["pony"].batch_size == 2


def test_old_fingerprints_do_not_acquire_new_preset_fields():
    current = Settings().presets["pony"]
    original = current.model_dump(
        mode="json",
        exclude={
            "batch_size",
            "primary_output_node",
            "resources",
            "extensions",
            "image_inputs",
            "parameters",
            "parameter_bounds",
            "seed_offsets",
            "output_stages",
            "separate_subjects",
        },
    )
    old = GenerationPreset.model_validate(original)
    assert old.batch_size == 1 and old.primary_output_node is None
    assert preset_fingerprint(old, 2) == digest(original)
    assert compile_image_input(current, "a cabin").compiler_version == 4


def test_partial_retry_never_hides_already_saved_candidates(
    project, session, client, stub, monkeypatch
):
    active = session.model_copy(update={"settings": project.settings()})
    run = submit_generation(project, client, active, "a cabin", None, 42, candidates=4)
    stub.invalid_images.add("candidate-2.png")
    assert collect(project, client, active).errors == (run.run_id,)
    partial = project.repository.read(run.run_id)
    assert len(partial.images) == 2 and partial.state == "queued"
    available = partial.first_candidate_available_at
    assert available and partial.image_metadata[partial.images[0]].saved_at == available
    sizes = []
    repository = project.repository
    original = type(repository).save

    def save(self, value):
        if value.run_id == run.run_id:
            sizes.append(len(value.images))
        return original(self, value)

    monkeypatch.setattr(type(repository), "save", save)
    assert collect(project, client, active).errors == (run.run_id,)
    assert min(sizes) == 2
    stub.invalid_images.clear()
    assert collect(project, client, active).saved == 1
    assert len(project.repository.read(run.run_id).images) == 4
    final = project.repository.read(run.run_id)
    assert final.first_candidate_available_at == available
    assert final.image_metadata[final.images[0]].saved_at == available
    events = [
        json.loads(line)
        for path in project.logs.rglob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert sum(row["operation"] == "image_availability" for row in events) == 1


def test_missing_primary_images_remain_retryable_without_discarding_valid_output(
    project, session, client, stub
):
    active = session.model_copy(update={"settings": project.settings()})
    run = submit_generation(project, client, active, "a cabin", None, 42, candidates=2)
    record = stub.history[run.prompt_id]
    complete = deepcopy(record["outputs"]["9"]["images"])
    record["outputs"]["9"]["images"].pop()
    assert collect(project, client, active).errors == (run.run_id,)
    partial = project.repository.read(run.run_id)
    assert len(partial.images) == 1 and partial.failure.code == "invalid_output"
    record["outputs"]["9"]["images"] = complete
    assert collect(project, client, active).saved == 1


def test_upscale_base_images_are_auxiliary_not_extra_candidates(project, session, client, stub):
    settings = project.settings().model_copy(update={"profiles": ("pony-upscale",)})
    active = session.model_copy(update={"settings": settings})
    run = submit_generation(
        project, client, active, "a cabin", None, 42, "pony-upscale", candidates=2
    )
    record = stub.history[run.prompt_id]
    record["outputs"]["16"] = deepcopy(record["outputs"]["9"])
    assert collect(project, client, active).saved == 1
    saved = project.repository.read(run.run_id)
    assert len(saved.images) == 4 and saved.output_spec.primary_node == "16"
    assert sum(meta.role == "candidate" for meta in saved.image_metadata.values()) == 2
    assert sum(meta.role == "auxiliary" for meta in saved.image_metadata.values()) == 2
    candidates = [meta for meta in saved.image_metadata.values() if meta.role == "candidate"]
    assert saved.first_candidate_available_at == candidates[0].saved_at


def test_legacy_collection_does_not_invent_unobserved_availability(project, session, client):
    active = session.model_copy(update={"settings": project.settings()})
    run = submit_generation(project, client, active, "a cabin", None, 42)
    run.format_version = 4
    project.repository.save(run)
    assert collect(project, client, active).saved == 1
    saved = project.repository.read(run.run_id)
    assert saved.first_candidate_available_at is None
    assert all(meta.saved_at is None for meta in saved.image_metadata.values())


def test_mutated_remote_reference_does_not_overwrite_a_saved_checkpoint(
    project, session, client, stub
):
    active = session.model_copy(update={"settings": project.settings()})
    run = submit_generation(project, client, active, "a cabin", None, 42, candidates=2)
    stub.invalid_images.add("candidate-1.png")
    collect(project, client, active)
    previous = project.repository.read(run.run_id).image_metadata["images/0001.png"]
    stub.history[run.prompt_id]["outputs"]["9"]["images"][0]["filename"] = "changed.png"
    result = collect(project, client, active)
    assert result.errors == (run.run_id,)
    retained = project.repository.read(run.run_id)
    assert retained.image_metadata["images/0001.png"] == previous
    assert retained.failure.code == "invalid_output"


def test_malformed_browser_topology_has_no_invented_candidate_role(project):
    from komaquill.application.generation import output_spec

    settings = project.settings()
    graph = deepcopy(settings.presets["pony"].api)
    graph["3"]["inputs"] = None
    assert output_spec(settings, graph) is None
    graph = deepcopy(settings.presets["pony"].api)
    graph["3"]["class_type"] = "UnknownSampler"
    assert output_spec(settings, graph) is None


@pytest.mark.parametrize("text", ["", " ", "\n"])
def test_empty_positive_is_rejected_by_the_shared_generation_boundary(
    project, session, client, stub, text
):
    with pytest.raises(KomaquillError):
        submit_generation(project, client, session, text, None, 42)
    assert stub.submissions == 0 and not project.repository.load()


def test_unsupported_legacy_batch_override_is_rejected_before_post(project, session, client, stub):
    settings = project.settings()
    legacy = settings.presets["pony"].model_copy(update={"primary_output_node": None})
    active = session.model_copy(
        update={
            "settings": settings.model_copy(
                update={"presets": {**settings.presets, "pony": legacy}}
            )
        }
    )
    with pytest.raises(KomaquillError) as error:
        submit_generation(project, client, active, "a cabin", None, 42, candidates=2)
    assert error.value.code == "unsupported_batch" and stub.submissions == 0


def test_invalid_default_candidate_limit_fails_before_runtime_allocation():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(max_candidates=1)


def test_failed_generation_keeps_request_identity_and_retry_advice(
    project, session, client, stub, monkeypatch
):
    from komaquill.application.generation import generate
    from komaquill.messages import error_message

    original = client.submit

    def submit(*args):
        identity = original(*args)
        stub.history[identity]["status"] = {"status_str": "error", "completed": True}
        return identity

    monkeypatch.setattr(client, "submit", submit)
    with pytest.raises(KomaquillError) as error:
        generate(project, client, session, "a cabin", None, 42, candidates=2)
    assert error.value.details["run_id"] == str(project.repository.load()[0].run_id)
    assert "fewer candidates" in error_message(error.value)
    assert error.value.details["run_id"] in error_message(error.value)
