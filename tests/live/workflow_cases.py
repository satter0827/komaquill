"""Prepare fixed test materials and exercise the three reviewed workflow families."""

import json
import time
from pathlib import Path
from uuid import UUID

from PIL import Image, ImageDraw, ImageFilter

from komaquill.api import Client
from komaquill.domain.materials import InputArtifact
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.configuration import find_project


def workflow_probes(folder: Path) -> None:
    project = find_project()
    api = Client(project.root)
    references = {
        row["case"]: row for row in json.loads((folder / "references.json").read_bytes())["cases"]
    }
    person = json.loads((folder / "person-negative.json").read_bytes())["images"][1]
    cats = json.loads((folder / "cat-short.json").read_bytes())["images"]
    generated = folder / "fixtures"
    generated.mkdir(exist_ok=True)
    masks = {}
    for name in ["person", "kitten", "contact", "pair"]:
        image = Image.new("L", (1024, 1024), 0)
        drawing = ImageDraw.Draw(image)
        if name == "person":
            drawing.ellipse((60, 30, 1000, 530), fill=255)
            drawing.rounded_rectangle((80, 260, 650, 1010), radius=70, fill=255)
        elif name == "kitten":
            drawing.ellipse((620, 630, 950, 1000), fill=255)
        elif name == "contact":
            drawing.rounded_rectangle((390, 440, 1000, 1010), radius=70, fill=255)
        else:
            drawing.rounded_rectangle((180, 400, 1010, 1010), radius=80, fill=255)
        image = image.filter(ImageFilter.GaussianBlur(6))
        path = generated / f"{name}-mask.png"
        image.save(path)
        masks[name] = api.inputs.import_file(path, kind="mask")
    imported = {
        "person": api.inputs.import_file(person),
        "kitten": api.inputs.import_file(cats[1]),
        "pair": api.inputs.import_file(cats[0]),
        "street": api.inputs.import_file(references["street"]["images"][1]),
    }
    atomic_json(
        folder / "materials.json",
        {
            "format_version": 1,
            "purpose": "Fixed appearance references and edit masks for rainy-cat comparisons.",
            "inputs": {key: value.model_dump(mode="json") for key, value in imported.items()},
            "masks": {key: value.model_dump(mode="json") for key, value in masks.items()},
            "limitations": [
                "Person reference does not establish a correct umbrella grip.",
                "Pair reference came from an unexpected two-cat result for a single-cat prompt.",
            ],
        },
    )
    cases = [
        (
            "compose-probe",
            "story-compose",
            "rainy shop street, watercolor storybook illustration, soft muted colors",
            {
                "base": imported["street"],
                "reference": imported["person"],
                "cat_reference": imported["kitten"],
                "person_mask": masks["person"],
                "cat_mask": masks["kitten"],
            },
            (
                "young woman, short black bob, yellow raincoat, crouching, "
                "gripping the handle of a large blue umbrella angled to the right",
                "tiny feral gray kitten, white chest, four paws on the pavement, "
                "sitting, natural feline body",
            ),
        ),
        (
            "contact-probe",
            "story-inpaint",
            "woman's hand gripping a blue umbrella handle, a small feral gray kitten with "
            "a white chest shelters under the canopy to her right, natural cat anatomy, "
            "rainy pavement, watercolor illustration",
            {
                "base": imported["person"],
                "reference": imported["kitten"],
                "mask": masks["contact"],
            },
            (),
        ),
        (
            "pair-probe",
            "story-inpaint",
            "two feral gray domestic cats, large mother cat on right lowers her nose toward "
            "a tiny kitten on left, white chest patches, four paws each, natural feline anatomy, "
            "close together, full bodies on pavement under a shop awning, watercolor illustration",
            {
                "base": imported["street"],
                "reference": imported["pair"],
                "mask": masks["pair"],
            },
            (),
        ),
    ]
    for name, profile, prompt, inputs, subjects in cases:
        destination = folder / f"{name}.json"
        if destination.is_file():
            continue
        started = time.monotonic()
        run = api.images.generate(
            prompt,
            profile=profile,
            inputs=inputs,
            subjects=subjects,
            negative="anthro, watermark, text",
            seed=2300,
            candidates=2,
        )
        atomic_json(
            destination,
            {
                "case": name,
                "profile": profile,
                "run_id": str(run.run_id),
                "elapsed_seconds": time.monotonic() - started,
                "images": [f"runs/{run.run_id}/{image}" for image in run.images],
                "inputs": {key: value.model_dump(mode="json") for key, value in inputs.items()},
            },
        )
        print(json.dumps({"case": name, "run_id": str(run.run_id)}), flush=True)


def parameter_probes(folder: Path) -> None:
    """Compare one declared adapter strength while retaining all other inputs."""
    project = find_project()
    api = Client(project.root)
    for name in ("contact", "pair"):
        result_path = folder / f"{name}-no-adapter.json"
        if result_path.is_file():
            continue
        source = json.loads((folder / f"{name}-probe.json").read_bytes())
        original = project.repository.read(UUID(source["run_id"]))
        applied = original.compiled_input
        started = time.monotonic()
        run = api.images.generate(
            applied.body,
            negative=applied.negative_body,
            seed=2300,
            candidates=2,
            profile="story-inpaint",
            parameters={"reference_weight": 0.0},
            inputs={
                key: InputArtifact.model_validate(value) for key, value in source["inputs"].items()
            },
        )
        atomic_json(
            result_path,
            {
                "case": f"{name}-no-adapter",
                "run_id": str(run.run_id),
                "elapsed_seconds": time.monotonic() - started,
                "images": [f"runs/{run.run_id}/{image}" for image in run.images],
                "changed_conditions": ["reference_weight"],
            },
        )
        print(json.dumps({"case": name, "run_id": str(run.run_id)}), flush=True)


def sdxl_probes(folder: Path) -> None:
    """Use the already confirmed Base checkpoint for an image-only handoff comparison."""
    from komaquill.application.generation import generation_settings
    from komaquill.domain.images import compile_image_input
    from komaquill.infrastructure.asset_store import inventory
    from komaquill.infrastructure.sessions.connection import connected
    from komaquill.infrastructure.workflows import prepare
    from tests.live.story_workflows import graph_request

    project = find_project()
    asset = next(
        row for row in inventory(project.models) if row.filename == "sd_xl_base_1.0.safetensors"
    )
    for name in ("contact", "pair"):
        if (folder / f"{name}-sdxl.json").is_file():
            continue
        source = json.loads((folder / f"{name}-probe.json").read_bytes())
        original = project.repository.read(UUID(source["run_id"]))
        applied = original.compiled_input
        with connected(project) as (_, session):
            settings = generation_settings(
                session.settings, "story-inpaint", 2, {"reference_weight": 0.0}
            )
            profile = settings.presets["story-inpaint"]
            compiled = compile_image_input(
                profile, applied.body, applied.negative_body, inputs=applied.inputs
            )
            graph, ui = prepare(
                applied.body,
                applied.negative_body,
                2300,
                f"{name}-sdxl",
                settings=settings,
                profile_name="story-inpaint",
                compiled_input=compiled,
            )
        graph["4"]["inputs"]["ckpt_name"] = asset.reference
        graph["11"]["inputs"]["stop_at_clip_layer"] = -1
        graph["10"]["inputs"]["string_a"] = ""
        for node in ui["nodes"]:
            if node["id"] == 4:
                node["widgets_values"][0] = asset.reference
            if node["id"] == 11:
                node["widgets_values"][0] = -1
            if node["id"] == 10:
                node["widgets_values"][0] = ""
        result = graph_request(graph, ui, folder, f"{name}-sdxl")
        result["changed_conditions"] = ["checkpoint", "model_quality_prefix", "clip_skip"]
        atomic_json(folder / f"{name}-sdxl.json", result)
        print(json.dumps({"case": name, "run_id": result["run_id"]}), flush=True)
