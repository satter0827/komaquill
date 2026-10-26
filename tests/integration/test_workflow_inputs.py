"""Exercise image inputs, fixed production conditions and failed-output recovery."""

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from komaquill.api import Client, KomaquillError, RenderConfig, SceneRenderConfig, services, stories
from komaquill.application.generation import collect, submit_generation
from komaquill.infrastructure.stories.storage import FileStoryRepository
from komaquill.interfaces.cli import app
from tests.support.stories import source


def configure(project, session, client, monkeypatch):
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connected(_):
        yield client, active

    monkeypatch.setattr(services, "connected", connected)
    monkeypatch.setattr(stories, "connected", connected)
    return Client(project.root), active


def image_file(folder: Path, name="input.png", mode="RGB", size=(32, 32)):
    path = folder / name
    Image.new(mode, size, 255 if mode == "L" else "green").save(path)
    return path


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
    run = api.images.generate("a gray kitten", profile="klein-edit", inputs={"reference": item})
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
        api.images.generate("kitten", profile="klein-edit", inputs=inputs)
    assert stub.submissions == stub.upload_calls == 0


def test_scene_profiles_and_materials_are_frozen_and_resume_does_not_repeat(
    project, session, client, stub, monkeypatch
):
    api, active = configure(project, session, client, monkeypatch)
    plan = api.stories.plan(source(), images=4, llm_profile="fake")
    image = api.inputs.import_file(image_file(project.root))
    config = RenderConfig(
        common_prompt="rainy street, overcast light",
        scenes=tuple(
            SceneRenderConfig(
                scene=i,
                profile="klein-edit",
                prompt=f"scene {i}",
                inputs={"reference": image},
                parameters={"party_strength": 0.4 + i / 20},
            )
            for i in range(1, 5)
        ),
    )
    final = list(api.stories.render(plan.request.story_id, render_config=config, seed=80))[
        -1
    ].snapshot
    assert final.complete and stub.submissions == 4
    saved = FileStoryRepository(project).render(final.render_id)
    assert [row.preset["parameters"]["party_strength"] for row in saved.scenes] == pytest.approx(
        [
            0.45,
            0.5,
            0.55,
            0.6,
        ]
    )
    assert all(row.compiled_input.inputs["reference"] == image for row in saved.scenes)
    assert all(
        row.compiled_input.common_prompt == config.common_prompt
        and row.compiled_input.body.startswith(config.common_prompt)
        for row in saved.scenes
    )
    assert list(api.stories.resume(final.render_id))[-1].snapshot.complete
    assert stub.submissions == 4
    summary = api.stories.export(plan.request.story_id)
    assert all(run.run.input_sha256 for run in summary.runs)


def test_all_scenes_are_validated_before_any_request(project, session, client, stub, monkeypatch):
    api, active = configure(project, session, client, monkeypatch)
    plan = api.stories.plan(source(), images=4, llm_profile="fake")
    image = api.inputs.import_file(image_file(project.root))
    config = RenderConfig(
        scenes=tuple(
            SceneRenderConfig(
                scene=i, profile="klein-edit", inputs={"reference": image} if i != 4 else {}
            )
            for i in range(1, 5)
        )
    )
    with pytest.raises(KomaquillError):
        list(api.stories.render(plan.request.story_id, render_config=config))
    assert stub.submissions == 0


def test_failed_intermediate_collection_retries_without_promoting_success(
    project, session, client, stub
):
    active = session.model_copy(update={"settings": project.settings()})
    run = submit_generation(project, client, active, "a scene", None, 1)
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
    run = submit_generation(project, client, active, "a scene", None, 1)
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
        {"party_strength": True},
        {"party_strength": -11},
        {"party_strength": 10.1},
    ],
)
def test_invalid_stage_parameters_fail_before_upload(
    project, session, client, stub, monkeypatch, parameters
):
    api, _ = configure(project, session, client, monkeypatch)
    image = api.inputs.import_file(image_file(project.root))
    with pytest.raises(KomaquillError):
        api.images.generate(
            "a cat", profile="klein-edit", inputs={"reference": image}, parameters=parameters
        )
    assert stub.submissions == stub.upload_calls == 0


def test_cli_resolves_materials_relative_to_each_input_file(
    project, session, client, stub, monkeypatch
):
    api, _ = configure(project, session, client, monkeypatch)
    folder = project.root / "files"
    folder.mkdir()
    image_file(folder)
    path = folder / "image.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 2,
                "prompt": "a cat",
                "inputs": {"reference": "input.png"},
                "parameters": {"party_strength": 0.25},
            }
        )
    )
    result = CliRunner().invoke(
        app,
        [
            "--project",
            str(project.root),
            "generate",
            "--profile",
            "klein-edit",
            "--input-file",
            str(path),
        ],
    )
    assert result.exit_code == 0, result.output
    plan = api.stories.plan(source(), images=1, llm_profile="fake")
    path = folder / "render.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "scenes": [
                    {"scene": 1, "profile": "klein-edit", "inputs": {"reference": "input.png"}}
                ],
            }
        )
    )
    result = CliRunner().invoke(
        app,
        [
            "--project",
            str(project.root),
            "stories",
            "render",
            str(plan.request.story_id),
            "--render-file",
            str(path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert stub.submissions == 2
    path.write_text('{"scenes":[{}]}')
    result = CliRunner().invoke(
        app,
        [
            "--project",
            str(project.root),
            "stories",
            "render",
            str(plan.request.story_id),
            "--render-file",
            str(path),
        ],
    )
    assert result.exit_code != 0 and "Traceback" not in result.output
    assert stub.submissions == 2
