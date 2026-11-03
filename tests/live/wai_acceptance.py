"""Compare fixed WAI conditions under an explicitly owned bounded T4 session."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from komaquill.api import Client

from scripts._support import atomic_json

CASES = json.loads(Path(__file__).with_name("wai-scenes.json").read_bytes())


def collect(root: Path) -> None:
    output = root / "wai-comparison"
    output.mkdir(exist_ok=False)
    started = json.loads((root / "gpu-start.json").read_bytes())
    deadline = datetime.fromisoformat(started["started_at"]).timestamp() + started["max_seconds"]
    records = []
    reservations = []
    api = Client(root / "gpu-workspace")
    try:
        atomic_json(output / "conditions.json", {"cases": CASES, "seeds": [101, 307]})
        for name, purpose, baseline, candidate, negative in CASES:
            for seed in (101, 307):
                for variant, prompt in (("baseline", baseline), ("candidate", candidate)):
                    if datetime.now(UTC).timestamp() >= deadline - 900 or len(reservations) >= 24:
                        raise RuntimeError("The authorized image budget has been reached.")
                    identity = uuid5(
                        NAMESPACE_URL, str(output.resolve()) + f"/{name}/{seed}/{variant}"
                    )
                    reservations.append(str(identity))
                    atomic_json(output / "reservations.json", reservations)
                    profile = api.images._generation_workspace.settings().presets["wai-illustrious"]
                    requested_negative = (
                        profile.negative_default + ", " + negative
                        if variant == "candidate"
                        else None
                    )
                    run = api.images.submit(
                        prompt,
                        run_id=identity,
                        seed=seed,
                        model="wai-illustrious",
                        purpose=purpose,
                        negative=requested_negative,
                    )
                    run = api.images.wait(run.run_id)
                    api.images.collect(run.run_id)
                    run = api.images.get(run.run_id)
                    images = sorted(
                        (api.workspace / f".artifacts/data/runs/{identity}/images").glob("*.png")
                    )
                    record = {
                        "case": name,
                        "seed": seed,
                        "variant": variant,
                        "run": run.model_dump(mode="json"),
                        "images": [
                            {
                                "path": str(p.resolve()),
                                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                            }
                            for p in images
                        ],
                    }
                    records.append(record)
                    atomic_json(output / "results.json", {"results": records, "finished": False})
                    print(
                        json.dumps(
                            {"case": name, "seed": seed, "variant": variant, "state": run.state}
                        ),
                        flush=True,
                    )
                    if run.state != "saved":
                        raise RuntimeError("The image did not reach saved state.")
        atomic_json(output / "results.json", {"results": records, "finished": True})
    finally:
        try:
            status = api.runtime.stop(progress=lambda event: print(event, flush=True))
            usage = api.runtime.usage()
            atomic_json(
                output / "shutdown.json",
                {
                    "gpu": status.gpu,
                    "collector_running": status.collector_running,
                    "usage": usage.model_dump(mode="json"),
                    "finished_at": datetime.now(UTC).isoformat(),
                },
            )
        finally:
            api.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-root", required=True, type=Path)
    parser.add_argument("--allow-real-images", action="store_true", required=True)
    args = parser.parse_args()
    collect(args.report_root.resolve())
