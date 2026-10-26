"""Render fixed four-scene recipes without allocating a GPU or calling an LLM."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from komaquill.api import Client, RenderConfig, SceneRenderConfig
from komaquill.domain.materials import InputArtifact
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.colab import Colab
from komaquill.infrastructure.configuration import find_project

STORY_ID = UUID("55f5d880-ca9f-4128-93b6-4361fba48ca6")
SEEDS = (31415, 27182, 16180)


def execute(folder: Path) -> None:
    project = find_project()
    api = Client(project.root)
    assert Colab(project).receipt() is not None
    frozen = folder / "acceptance-recipe.json"
    if frozen.is_file():
        config = RenderConfig.model_validate_json(frozen.read_bytes())
    else:
        materials = json.loads((folder / "materials.json").read_bytes())
        inputs = {k: InputArtifact.model_validate(v) for k, v in materials["inputs"].items()}
        masks = {k: InputArtifact.model_validate(v) for k, v in materials["masks"].items()}
        back = api.inputs.import_file(
            project.data / "stories/20261025-rainy-cat-reviewed-55f5d880/renders/"
            "f907b660-0d00-4222-b6ed-33d68e1ea4dd/images/004-001-001.png"
        )
        config = RenderConfig(
            scenes=(
                SceneRenderConfig(
                    scene=1,
                    profile="story-compose",
                    prompt="rainy street, soft storybook illustration",
                    subjects=(
                        "young woman, short black bob, yellow raincoat, crouching, "
                        "holding one blue umbrella",
                        "one tiny feral (gray kitten:1.3), (white chest:1.2), "
                        "four paws on ground, sitting",
                    ),
                    inputs={
                        "base": inputs["street"],
                        "reference": inputs["person"],
                        "cat_reference": inputs["kitten"],
                        "person_mask": masks["person"],
                        "cat_mask": masks["kitten"],
                    },
                    parameters={
                        "reference_weight": 0.0,
                        "cat_weight": 0.0,
                        "person_denoise": 1.0,
                        "cat_denoise": 1.0,
                    },
                ),
                SceneRenderConfig(
                    scene=2,
                    profile="story-inpaint",
                    prompt="young woman in yellow raincoat grips a blue umbrella handle "
                    "with her hand, canopy shelters one tiny feral (gray kitten:1.3), "
                    "white chest, on the pavement to her right, human shoulder exposed to rain, "
                    "natural feline body, soft storybook illustration",
                    inputs={
                        "base": inputs["person"],
                        "reference": inputs["kitten"],
                        "mask": masks["contact"],
                    },
                    parameters={"reference_weight": 0.0, "denoise": 1.0},
                ),
                SceneRenderConfig(
                    scene=3,
                    profile="story-inpaint",
                    prompt="two feral (gray domestic cats:1.3), white chest patches, "
                    "natural feline bodies, "
                    "large mother on right bends her head toward tiny kitten on left, noses touch, "
                    "four paws each on the pavement beneath a shop awning, "
                    "soft storybook illustration",
                    inputs={
                        "base": inputs["street"],
                        "reference": inputs["pair"],
                        "mask": masks["pair"],
                    },
                    parameters={"reference_weight": 0.0, "denoise": 1.0},
                ),
                SceneRenderConfig(
                    scene=4,
                    profile="story-reference",
                    prompt="rear view of a young woman, short black bob, yellow raincoat, "
                    "walking away holding one blue umbrella overhead, empty rainy street, "
                    "soft storybook illustration",
                    inputs={"reference": back},
                    parameters={"reference_weight": 0.2},
                ),
            )
        )
        atomic_json(frozen, config.model_dump(mode="json"))
    destination = folder / "acceptance.json"
    report = (
        json.loads(destination.read_bytes())
        if destination.is_file()
        else {
            "format_version": 1,
            "started_at": datetime.now(UTC).isoformat(),
            "identity": project.identity(),
            "story_id": str(STORY_ID),
            "seeds": list(SEEDS),
            "renders": [],
            "decision": "not_evaluated",
            "evaluator": "assistant visual review; no real LLM selector",
        }
    )
    for seed in SEEDS:
        if any(row["seed"] == seed for row in report["renders"]):
            continue
        started = time.monotonic()
        for event in api.stories.render(STORY_ID, render_config=config, seed=seed, candidates=2):
            if event.snapshot:
                print(
                    json.dumps(
                        {
                            "seed": seed,
                            "render_id": str(event.snapshot.render_id),
                            "complete": event.snapshot.complete,
                        }
                    ),
                    flush=True,
                )
        snapshot = event.snapshot
        report["renders"].append(
            {
                "seed": seed,
                "render_id": str(snapshot.render_id),
                "complete": snapshot.complete,
                "elapsed_seconds": time.monotonic() - started,
            }
        )
        atomic_json(destination, report)
        atomic_json(
            folder / f"acceptance-{seed}-summary.json",
            api.stories.export(STORY_ID, render_id=snapshot.render_id).model_dump(mode="json"),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-gpu", required=True, action="store_true")
    parser.add_argument("--report", required=True, type=Path)
    execute(parser.parse_args().report)
