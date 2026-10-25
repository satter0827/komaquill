"""Run bounded image comparisons only on an explicitly started owned GPU."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from komaquill.api import Client
from komaquill.application.generation import new_run, wait_generation
from komaquill.domain.errors import KomaquillError, failure_info
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.colab import Colab
from komaquill.infrastructure.configuration import find_project
from komaquill.infrastructure.sessions.connection import connected


def reference_cases():
    style = "soft colored storybook illustration, clear silhouette, simple shapes, rainy day"
    return {
        "mio-front": (
            f"{style}, one human young woman, short black bob, yellow raincoat, crouching, "
            "human hands firmly holding the handle of a single blue umbrella, full body, "
            "side view, simple wet street, no animals"
        ),
        "mio-back": (
            f"{style}, rear view of one human young woman, short black bob, yellow raincoat, "
            "walking away, holding a single blue umbrella over her head, full body, no animals"
        ),
        "kitten": (
            f"{style}, solo feral domestic kitten, tiny gray kitten, white chest patch, "
            "natural feline body, on all fours, four paws on ground, small ears, "
            "side view, full body, sitting on wet pavement, no humans"
        ),
        "mother": (
            f"{style}, solo feral adult gray mother cat, white chest patch, natural feline "
            "body, on all fours, four paws on ground, side view, full body, no humans"
        ),
        "street": (
            f"{style}, empty rainy street, wet sidewalk, puddles, shop awning on the right, "
            "eye level, wide empty foreground, no people, no animals, no umbrellas"
        ),
        "awning": (
            f"{style}, empty dry pavement beneath a shop awning, rainy street beyond, "
            "eye level close view, no people, no animals, no umbrellas"
        ),
    }


def references(folder: Path) -> None:
    project = find_project()
    api = Client(project.root)
    assert Colab(project).receipt() is not None
    path = folder / "references.json"
    result = (
        json.loads(path.read_bytes())
        if path.is_file()
        else {
            "format_version": 1,
            "started_at": datetime.now(UTC).isoformat(),
            "identity": project.identity(),
            "cases": [],
        }
    )
    done = {item["case"] for item in result["cases"]}
    for index, (name, prompt) in enumerate(reference_cases().items()):
        if name in done:
            continue
        negative = (
            "anthro, humanoid, human body, clothes, dress, biped, extra animals"
            if name in {"kitten", "mother"}
            else "extra people, extra umbrellas, extra limbs, floating umbrella, animals"
        )
        started = time.monotonic()
        run = api.images.generate(
            prompt, negative=negative, seed=1200 + index, profile="pony", candidates=2
        )
        result["cases"].append(
            {
                "case": name,
                "run_id": str(run.run_id),
                "batch_seed": 1200 + index,
                "elapsed_seconds": time.monotonic() - started,
                "images": [
                    str(api.artifact_path(f"runs/{run.run_id}/{image}")) for image in run.images
                ],
            }
        )
        atomic_json(path, result)
        print(json.dumps({"case": name, "run_id": str(run.run_id)}), flush=True)


def graph_request(graph: dict, ui: dict, folder: Path, case: str) -> dict:
    """Keep exploratory graphs in normal run storage before sending any prompt."""
    project = find_project()
    identity = uuid4()
    started = time.monotonic()
    with connected(project) as (backend, session):
        run = new_run(project, session, identity, graph)
        run.state = "unknown"
        with project.repository.lock():
            project.repository.prepare(run, graph, ui)
            project.repository.save(run)
        try:
            prompt_id = backend.submit(graph, ui, str(identity))
            with project.repository.lock():
                current = project.repository.read(identity)
                current.prompt_id = prompt_id
                if current.pending:
                    current.state = "queued"
                project.repository.save(current)
            run = wait_generation(project, backend, session, identity)
        except KomaquillError as error:
            with project.repository.lock():
                current = project.repository.read(identity)
                if (
                    error.http_status is not None
                    and 400 <= error.http_status < 500
                    and current.pending
                ):
                    current.state = "failed"
                    current.failure = failure_info(error, "submission")
                    project.repository.save(current)
            raise
        finally:
            run = project.repository.read(identity)
            result = {
                "case": case,
                "run_id": str(identity),
                "state": run.state,
                "identity": project.identity(),
                "elapsed_seconds": time.monotonic() - started,
                "images": [f"runs/{identity}/{image}" for image in run.images],
                "workflow_sha256": run.workflow_sha256,
            }
            atomic_json(folder / f"{case}.json", result)
        return result


def prompt_probes(folder: Path) -> None:
    api = Client(find_project().root)
    cases = [
        (
            "person-negative",
            reference_cases()["mio-front"],
            "extra limbs, bad anatomy, watermark, text",
            1200,
        ),
        (
            "cat-short",
            "feral, domestic kitten, (gray fur:1.3), (white chest:1.2), "
            "quadruped, sitting, side view, full body, flat natural light, storybook illustration",
            "anthro, humanoid, clothes, biped, extra legs",
            1202,
        ),
    ]
    for name, prompt, negative, seed in cases:
        destination = folder / f"{name}.json"
        if destination.is_file():
            continue
        started = time.monotonic()
        run = api.images.generate(
            prompt, negative=negative, seed=seed, profile="pony", candidates=2
        )
        atomic_json(
            destination,
            {
                "case": name,
                "run_id": str(run.run_id),
                "batch_seed": seed,
                "elapsed_seconds": time.monotonic() - started,
                "images": [
                    str(api.artifact_path(f"runs/{run.run_id}/{image}")) for image in run.images
                ],
            },
        )
        print(json.dumps({"case": name, "run_id": str(run.run_id)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-gpu", action="store_true", required=True)
    parser.add_argument(
        "--phase",
        choices=["references", "prompt-probes", "workflow-probes", "parameter-probes"],
        required=True,
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.report.mkdir(parents=True, exist_ok=True)
    if args.phase == "references":
        references(args.report)
    elif args.phase == "prompt-probes":
        prompt_probes(args.report)
    elif args.phase == "workflow-probes":
        from tests.live.workflow_cases import workflow_probes

        workflow_probes(args.report)
    else:
        from tests.live.workflow_cases import parameter_probes

        parameter_probes(args.report)
