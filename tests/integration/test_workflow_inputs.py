"""Exercise image inputs, fixed production conditions and failed-output recovery."""

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest
from komaquill.api import Client, KomaquillError
from komaquill.api import images as services
from komaquill.images.generation import collect, submit_generation
from PIL import Image


def configure(project, session, client, monkeypatch):
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connected(_):
        yield client, active

    monkeypatch.setattr(services, "connected", connected)
    return Client(project.root), active


def image_file(folder: Path, name="input.png", mode="RGB", size=(32, 32)):
    path = folder / name
    Image.new(mode, size, 255 if mode == "L" else "green").save(path)
    return path


def pinned_reference_run(project, session, client, stub, monkeypatch):
    api, active = configure(project, session, client, monkeypatch)
    item = api.inputs.import_file(image_file(project.root))
    run = submit_generation(
        project, client, active, "a scene", 1, "klein-4b-edit", inputs={"reference": item}
    )
    record = stub.history[run.prompt_id]
    pinned = f"komaquill/{item.sha256}/{Path(item.reference).name}"
    for node in record["prompt"][2].values():
        if node["class_type"] == "LoadImage":
            node["inputs"]["image"] = pinned
    record["prompt"][3]["komaquill"]["input_sha256"] = {pinned: item.sha256}
    return api, active, run, record, item


def test_observed_graph_and_hash_match_after_server_pins_a_reference(
    project, session, client, stub, monkeypatch
):
    from komaquill.domain.workflow import digest

    api, active, run, record, item = pinned_reference_run(
        project, session, client, stub, monkeypatch
    )
    pending = api.evaluations.export(run.run_id)["runs"][0]
    assert pending["workflow_api"] is None and pending["workflow_integrity"] is None
    assert collect(project, client, active).saved == 1
    saved = project.repository.read(run.run_id)
    observed = api.artifact_path(f"runs/{run.run_id}/workflow-api.json")
    assert digest(json.loads(observed.read_bytes())) == saved.workflow_sha256
    submitted = json.loads(
        api.artifact_path(f"runs/{run.run_id}/submitted-workflow-api.json").read_bytes()
    )
    assert submitted["77"]["inputs"]["image"] == item.reference
    assert json.loads(observed.read_bytes())["77"]["inputs"]["image"] != item.reference
    assert api.evaluations.export(run.run_id)["runs"][0]["workflow_integrity"] is True
    before = observed.read_bytes(), saved.workflow_sha256
    record["prompt"][2]["77"]["is_changed"] = ["runtime cache metadata"]
    project.repository.conditions(saved, record["prompt"])
    assert (observed.read_bytes(), project.repository.read(run.run_id).workflow_sha256) == before


@pytest.mark.parametrize(
    "change",
    ["input", "class", "nodes", "invalid_node", "invalid_incoming_node", "list", "corrupt"],
)
def test_changed_or_corrupt_observed_graph_does_not_rewrite_a_saved_run(
    project, session, client, stub, monkeypatch, change
):
    api, active, run, record, _ = pinned_reference_run(project, session, client, stub, monkeypatch)
    assert collect(project, client, active).saved == 1
    saved = project.repository.read(run.run_id)
    observed = api.artifact_path(f"runs/{run.run_id}/workflow-api.json")
    prompt = deepcopy(record["prompt"])
    if change == "input":
        prompt[2]["77"]["inputs"]["image"] = "a-different-image.png"
    elif change == "class":
        prompt[2]["77"]["class_type"] = "LoadImageMask"
    elif change == "nodes":
        prompt[2].pop("77")
    elif change == "invalid_incoming_node":
        prompt[2]["77"] = []
    elif change == "invalid_node":
        value = json.loads(observed.read_bytes())
        value["77"] = []
        observed.write_text(json.dumps(value))
    else:
        observed.write_text("[]" if change == "list" else "invalid JSON")
    before = observed.read_bytes(), saved.model_dump_json()
    with pytest.raises(KomaquillError) as caught:
        project.repository.conditions(saved, prompt)
    assert caught.value.code == ("storage_failed" if change == "corrupt" else "invalid_output")
    assert (observed.read_bytes(), project.repository.read(run.run_id).model_dump_json()) == before


def test_observed_graph_recovers_after_a_run_checkpoint_failure(
    project, session, client, stub, monkeypatch
):
    from komaquill.adapters import storage
    from komaquill.domain.workflow import digest

    api, _, run, record, _ = pinned_reference_run(project, session, client, stub, monkeypatch)
    original_hash = run.workflow_sha256

    def interrupted(*args):
        raise OSError("Injected checkpoint failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(storage, "save_run", interrupted)
        with pytest.raises(OSError):
            project.repository.conditions(run, record["prompt"])
    observed = api.artifact_path(f"runs/{run.run_id}/workflow-api.json")
    assert observed.is_file()
    assert project.repository.read(run.run_id).workflow_sha256 == original_hash
    recovered = project.repository.read(run.run_id)
    record["prompt"][2]["77"]["is_changed"] = ["later cache observation"]
    project.repository.conditions(recovered, record["prompt"])
    assert project.repository.read(run.run_id).workflow_sha256 == digest(
        json.loads(observed.read_bytes())
    )
    assert project.repository.read(run.run_id).state == "queued"
    assert api.artifact_path(f"runs/{run.run_id}/workflow.json").is_file()


def test_import_preserves_content_and_freezes_input_before_upload(
    project, session, client, stub, monkeypatch
):
    api, active = configure(project, session, client, monkeypatch)
    path = image_file(project.root)
    original = path.read_bytes()
    item = api.inputs.import_file(path)
    assert api.artifact_path(item.key).read_bytes() == original
    assert item == api.inputs.import_file(path)
    path.write_bytes(b"changed after import")
    run = api.images.generate(
        "a gray kitten", model="klein-4b", operation="edit", inputs={"reference": item}
    )
    assert run.state == "saved" and run.compiled_input.inputs == {"reference": item}
    assert stub.uploaded_inputs[item.reference] == original and stub.upload_calls == 1
    assert run.input_sha256[item.reference] == item.sha256
    assert str(project.root) not in run.compiled_input.model_dump_json()
    assert api.artifact_path(f"runs/{run.run_id}/workflow-api.json").is_file()


@pytest.mark.parametrize("case", ["missing", "extra", "corrupt"])
def test_invalid_materials_fail_before_generation(
    project, session, client, stub, monkeypatch, case
):
    api, _ = configure(project, session, client, monkeypatch)
    image = api.inputs.import_file(image_file(project.root))
    inputs = {"reference": image}
    if case == "missing":
        inputs.clear()
    elif case == "extra":
        inputs["other"] = image
    else:
        api.artifact_path(image.key).write_bytes(b"damaged")
    with pytest.raises(KomaquillError):
        api.images.generate("kitten", model="klein-4b", operation="edit", inputs=inputs)
    assert stub.submissions == stub.upload_calls == 0


def test_failed_intermediate_collection_retries_without_promoting_success(
    project, session, client, stub
):
    active = session.model_copy(update={"settings": project.settings()})
    run = submit_generation(project, client, active, "a scene", 1)
    record = stub.history[run.prompt_id]
    record["outputs"] = {"29": record["outputs"]["9"]}
    record["status"] = {"completed": False, "status_str": "error"}
    stub.image_fails = True
    assert run.run_id in collect(project, client, active).errors
    failed = project.repository.read(run.run_id)
    assert failed.state == "failed" and failed.collection_failure is not None
    original_failure = failed.failure
    stub.image_fails = False
    assert collect(project, client, active).saved == 0
    recovered = project.repository.read(run.run_id)
    assert recovered.images and recovered.state == "failed" and recovered.saved_at is None
    assert recovered.failure == original_failure and recovered.collection_failure is None
    requests = stub.image_requests
    collect(project, client, active)
    assert stub.image_requests == requests


def test_failed_outputs_keep_names_when_an_earlier_stage_appears_later(
    project, session, client, stub
):
    active = session.model_copy(update={"settings": project.settings()})
    run = submit_generation(project, client, active, "a scene", 1)
    record = stub.history[run.prompt_id]
    original = record["outputs"]["9"]
    record["outputs"] = {"29": original}
    record["status"] = {"completed": False, "status_str": "error"}
    collect(project, client, active)
    first = project.repository.read(run.run_id)
    original_names = list(first.images)
    record["outputs"] = {"9": original, "29": original}
    assert not collect(project, client, active).errors
    final = project.repository.read(run.run_id)
    assert final.state == "failed" and len(final.images) == 2 * len(original_names)
    assert final.images[: len(original_names)] == original_names
    assert all(final.image_metadata[name].output_node == "29" for name in original_names)
    calls = stub.image_requests
    collect(project, client, active)
    assert stub.image_requests == calls


@pytest.mark.parametrize(
    "parameters",
    [
        {"missing": 1},
        {"aniedit_strength": True},
        {"aniedit_strength": -11},
        {"aniedit_strength": 10.1},
    ],
)
def test_invalid_stage_parameters_fail_before_upload(
    project, session, client, stub, monkeypatch, parameters
):
    api, _ = configure(project, session, client, monkeypatch)
    image = api.inputs.import_file(image_file(project.root))
    with pytest.raises(KomaquillError):
        api.images.generate(
            "a cat",
            model="klein-4b",
            operation="edit",
            inputs={"reference": image},
            parameters=parameters,
        )
    assert stub.submissions == stub.upload_calls == 0


@pytest.mark.parametrize("filename", ["content", "image.bin", "source.png"])
def test_import_uses_real_image_format_for_standard_ui_choices(project, filename):
    from komaquill.adapters.inputs import import_file
    from komaquill.domain.materials import InputArtifact
    from PIL import Image

    path = project.root / filename
    Image.new("RGB", (16, 16), "blue").save(path, format="PNG")
    item = import_file(project, path, "image")
    assert item.name == filename
    assert item.format_version == 3 and item.reference.endswith(".png")
    assert item.reference == f"komaquill-{item.sha256}.png"
    assert InputArtifact.model_validate_json(item.model_dump_json()) == item
    old = item.model_dump(exclude={"extension"}) | {"format_version": 2}
    retained = InputArtifact.model_validate(old)
    assert retained.reference == f"komaquill-{item.sha256}-{filename}"
    assert retained.model_dump() == old
