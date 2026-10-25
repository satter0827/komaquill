"""Check fixed workflow inputs and resource preparation without invoking GPU libraries."""

import subprocess
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from komaquill.domain.errors import KomaquillError
from komaquill.domain.materials import InputArtifact
from komaquill.gpu.extensions import prepare_extensions
from komaquill.infrastructure.configuration import Settings
from komaquill.infrastructure.workflows import assets, prepare


@pytest.mark.parametrize("exists,mismatch", [(False, False), (True, False), (True, True)])
def test_reviewed_extension_revisions_are_checked(tmp_path, monkeypatch, exists, mismatch):
    settings = Settings(profiles=["story-inpaint"])
    names = ["ipadapter", "crop-stitch"]
    if exists:
        for name in names:
            (tmp_path / "custom_nodes" / f"komaquill-{name}").mkdir(parents=True)
    commands = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: commands.append(args))
    catalog = tomllib.loads(
        files("komaquill").joinpath("resources/runtime/extensions.toml").read_text()
    )
    revisions = {f"komaquill-{name}": catalog[name]["revision"] for name in names}
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda args, **kwargs: "wrong" if mismatch else revisions[Path(args[2]).name],
    )
    if mismatch:
        with pytest.raises(KomaquillError) as caught:
            prepare_extensions(tmp_path, settings)
        assert caught.value.code == "identity_mismatch"
    else:
        prepare_extensions(tmp_path, settings)
        assert len(commands) == (0 if exists else 4)


def test_composition_uses_independent_subjects_and_preserves_candidate_axis():
    settings = Settings(profiles=["story-compose"])
    graph, _ = prepare(
        "rainy street",
        None,
        99,
        "example",
        settings=settings,
        subject_prompts=("woman holding a blue umbrella", "feral gray kitten"),
    )
    profile = settings.presets["story-compose"]
    assert graph["10"]["inputs"]["string_b"] == "rainy street"
    assert graph["61"]["inputs"]["string_b"] == "woman holding a blue umbrella"
    assert graph["62"]["inputs"]["string_b"] == "feral gray kitten"
    assert graph["55"]["inputs"]["seed"] == 100
    assert graph["40"]["inputs"]["amount"] == profile.batch_size
    assert graph["58"]["inputs"]["destination"] == ["34", 0]
    assert graph["54"]["inputs"]["pixels"] == ["34", 0]
    assert {asset.name for asset in assets(settings)} == {
        "pony",
        "ipadapter-sdxl",
        "clip-vision-vit-h",
    }


def test_input_reference_versions_and_masked_workflow_labels():
    item = InputArtifact(
        key=f"inputs/{'a' * 64}/content",
        sha256="a" * 64,
        name="mask.png",
        kind="mask",
        size_bytes=10,
        width=32,
        height=32,
    )
    assert "/" not in item.reference and item.sha256 in item.reference
    assert (
        item.model_copy(update={"format_version": 1}).reference == f"komaquill/{'a' * 64}/mask.png"
    )
    settings = Settings(profiles=["story-inpaint"])
    graph, ui = prepare("a cat", None, 8, "mask-test", settings=settings)
    assert graph["3"]["inputs"]["denoise"] == 1.0
    node = next(node for node in ui["nodes"] if node["id"] == 31)
    assert next(slot for slot in node["inputs"] if slot["name"] == "image")["label"] == "Edit Mask"
    assert graph["35"]["inputs"]["destination"] == ["40", 0]
    assert graph["35"]["inputs"]["mask"] == ["31", 0]


def test_workflow_specific_inputs_require_bindings():
    preset = Settings(profiles=["story-reference"]).presets["story-reference"]
    missing = preset.model_dump()
    missing["bindings"] = [row for row in missing["bindings"] if row["source"] != "input_reference"]
    with pytest.raises(ValueError):
        type(preset).model_validate(missing)
