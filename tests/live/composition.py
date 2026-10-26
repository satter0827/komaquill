"""Run bounded composition cases on an explicitly allocated, separate workspace."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from komaquill.api import Client, Evaluation, Evaluator
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.configuration import Project

MATERIALS = {
    "pair-d": (
        "feral, two domestic cats, (gray fur:1.3), (white chest:1.2), quadruped, "
        "a large mother cat bending down to greet a small kitten, side view, full body, "
        "flat natural light, storybook illustration",
        "anthro, humanoid, clothes, biped, extra legs, dog, wolf, extra cats, extra faces",
        35008,
    ),
    "pair-c": (
        "two (domestic shorthair cats:1.5), (small rounded ears:1.3), short feline muzzles, "
        "pink noses, one adult gray mother cat and one tiny gray kitten, white chest patches, "
        "mother bending her head down to sniff the kitten, natural four legged cat bodies, "
        "side view, full bodies, muted cool light, painted storybook illustration, gray background",
        "dog, wolf, canine, fox, collar, anthro, clothes, extra cats, extra faces, orange fur",
        35007,
    ),
    "pair-a": (
        "feral, two gray cats, (gray fur:1.3), white chest patches, adult mother cat on right "
        "and much smaller kitten on left, touching noses, side view, full bodies, four paws "
        "on ground, soft painted storybook illustration, muted cool overcast light, "
        "plain dark gray background",
        "anthro, human, clothes, orange fur, brown fur, extra cats, cropped paws, text",
        35001,
    ),
    "pair-b": (
        "feral, mother cat nuzzling her kitten, two natural domestic cats, gray fur, white "
        "chest, mother twice the height of kitten, noses touching, side view, full bodies, "
        "soft painted storybook illustration, muted cool rainy daylight, plain gray background",
        "anthro, human body, clothes, orange fur, extra cats, floating paws, text",
        35002,
    ),
    "person-a": (
        "one young woman, short black bob, yellow raincoat, crouching, her right hand "
        "firmly wrapped around the curved handle of one blue umbrella, visible fingers "
        "gripping handle, umbrella over her right side, side view, full body, boots, "
        "soft painted storybook illustration, muted cool rainy daylight, plain gray background",
        "extra people, extra hands, extra umbrellas, floating umbrella, yellow umbrella, cats",
        35003,
    ),
    "person-b": (
        "side view of one crouching young woman with short black bob, yellow raincoat, "
        "one hand gripping a single blue umbrella handle beside her face, blue canopy "
        "tilted to shelter the space to her right, full body, boots on ground, "
        "soft painted storybook illustration, muted cool overcast light, plain gray background",
        "extra people, extra limbs, floating umbrella, yellow umbrella, animals",
        35004,
    ),
    "back": (
        "rear view, one young woman, short black bob, yellow raincoat, boots, walking away, "
        "right hand holding the handle of one blue umbrella above her head, full body, "
        "soft painted storybook illustration, muted cool rainy daylight, plain gray background",
        "extra people, extra umbrellas, yellow umbrella, red hair, animals",
        35005,
    ),
    "kitten": (
        "feral, one tiny gray domestic kitten, white chest patch, natural feline body, "
        "sitting on four paws, side view, full body, soft painted storybook illustration, "
        "muted cool rainy daylight, plain gray background",
        "anthro, human, clothes, extra animals, orange fur, oversized head, text",
        35006,
    ),
}

CRITERIA = (
    "Check visible subject count and species, fixed appearance, physical contact, ground "
    "and lighting continuity, weather and correspondence to the unchanged story. "
    "Unknown is not a pass. Record limitations of fixed manually verified materials."
)
MAX_MATERIAL_CASES = 6
MATERIAL_SESSION_SECONDS = 3600


def review(workspace: Path, run_id, image: str, decision: str, reason: str, case_id: str):
    """Register image reviews in the ordinary append-only evaluation repository."""
    api = Client(workspace)
    previous = next(
        (
            row
            for row in reversed(api.evaluations.list(run_id))
            if row.case_id == case_id and row.image == image
        ),
        None,
    )
    if previous and previous.decision == decision and previous.reason == reason:
        return previous
    row = Evaluation(
        run_id=run_id,
        image=image,
        criterion="story-composition",
        criterion_version="1",
        case_id=case_id,
        decision=decision,
        reason=reason,
        supersedes=previous.evaluation_id if previous else None,
        evaluator=Evaluator(
            kind="ai", name="Codex visual review", model="unknown", instruction=CRITERIA
        ),
    )
    api.evaluations.add([row])
    return row


def material(workspace: Path, report: Path, name: str) -> None:
    """Never resend an incomplete case whose acceptance is not yet reconciled."""
    destination = report / "materials" / f"{name}.json"
    if destination.exists():
        raise RuntimeError("A recorded case must be inspected before another submission.")
    prompt, negative, seed = MATERIALS[name]
    project = Project(workspace)
    monitor = json.loads((project.state / "monitor.json").read_bytes())
    if monitor.get("phase") != "ready":
        raise RuntimeError("Start the dedicated GPU workspace explicitly before this case.")
    created = datetime.fromisoformat(monitor["created_at"].replace("Z", "+00:00"))
    if (datetime.now(UTC) - created).total_seconds() > MATERIAL_SESSION_SECONDS:
        raise RuntimeError("The material-session time budget is exhausted; stop the GPU.")
    if len(list(destination.parent.glob("*.json"))) >= MAX_MATERIAL_CASES:
        raise RuntimeError("The material image budget is exhausted.")
    atomic_json(destination, {"case": name, "state": "submitting", "seed": seed})
    started = time.monotonic()
    run = Client(workspace).images.generate(
        prompt, negative=negative, profile="pony", candidates=2, seed=seed
    )
    atomic_json(
        destination,
        {
            "case": name,
            "state": run.state,
            "run_id": str(run.run_id),
            "seed": seed,
            "elapsed_seconds": time.monotonic() - started,
            "saved_at": run.saved_at.isoformat() if run.saved_at else None,
            "images": [f"runs/{run.run_id}/{image}" for image in run.images],
            "decision": "not_evaluated",
        },
    )
    print(json.dumps({"case": name, "state": run.state, "run_id": str(run.run_id)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-gpu", action="store_true", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--material", choices=MATERIALS, required=True)
    args = parser.parse_args()
    material(args.workspace, args.report, args.material)
