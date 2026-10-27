"""Measure candidate batches on an explicitly started owned GPU session."""

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from komaquill.api import Client, KomaquillError
from komaquill.application.generation import submit_generation, wait_generation
from komaquill.infrastructure.configuration import find_project
from komaquill.infrastructure.sessions.connection import connected


def measure(story_id: UUID) -> Path:
    project = find_project()
    api = Client(project.root)
    folder = project.reports / "batch-candidates" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True)
    report = {
        "format_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "identity": project.identity(),
        "story_id": str(story_id),
        "measurements": [],
        "sampling_note": (
            "First local image is observed at 100 ms intervals; "
            "VRAM values are endpoint snapshots, not a peak measurement. "
            "Cross-clock queue and collection deltas are estimates; clock offset is unknown."
        ),
    }
    path = folder / "measurement.json"

    def save():
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    positive = folder / "positive.txt"
    positive.write_text(
        "a small cabin in a sunny forest, illustration, warm light", encoding="utf-8"
    )
    before = {run.run_id for run in project.repository.load()}
    warmup = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "komaquill",
            "--project",
            str(project.root),
            "generate",
            "--prompt-file",
            str(positive),
            "--candidates",
            "1",
            "--seed",
            "100",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
    )
    (folder / "warmup.txt").write_text(warmup.stdout + warmup.stderr, encoding="utf-8")
    assert warmup.returncode == 0, "CLI warmup did not complete; inspect warmup.txt."
    report["warmup_run_ids"] = [
        str(run.run_id) for run in project.repository.load() if run.run_id not in before
    ]
    save()
    with connected(project) as (backend, session):
        report["runtime_id"] = str(session.runtime_id)
        for candidates in (1, 2, 4):
            started = time.monotonic()
            group = {"candidates": candidates, "target_images": 4, "runs": [], "failure": None}
            report["measurements"].append(group)
            try:
                for index in range(4 // candidates):
                    begin = time.monotonic()
                    memory_before = backend.json("/system_stats").get("devices")
                    run = submit_generation(
                        project,
                        backend,
                        session,
                        positive.read_text(encoding="utf-8"),
                        200 + index,
                        "klein-4b",
                        candidates=candidates,
                    )
                    observed = []
                    done = threading.Event()

                    def observe(identity=run.run_id, origin=begin, done=done, observed=observed):
                        while not done.is_set():
                            if project.repository.read(identity).images:
                                observed.append(time.monotonic() - origin)
                                return
                            done.wait(0.1)

                    thread = threading.Thread(target=observe, daemon=True)
                    thread.start()
                    try:
                        saved = wait_generation(project, backend, session, run.run_id)
                    finally:
                        done.set()
                        thread.join(timeout=2)
                    group["runs"].append(
                        {
                            "run_id": str(saved.run_id),
                            "batch_seed": 200 + index,
                            "elapsed_seconds": time.monotonic() - begin,
                            "first_observed_image_seconds": observed[0] if observed else None,
                            "queue_and_submission_seconds": (
                                saved.generation_started_at - saved.created_at
                            ).total_seconds()
                            if saved.generation_started_at
                            and saved.generation_started_at >= saved.created_at
                            else None,
                            "gpu_seconds": (
                                saved.generation_completed_at - saved.generation_started_at
                            ).total_seconds()
                            if saved.generation_started_at
                            and saved.generation_completed_at
                            and saved.generation_completed_at >= saved.generation_started_at
                            else None,
                            "collection_seconds": (
                                saved.saved_at - saved.generation_completed_at
                            ).total_seconds()
                            if saved.saved_at
                            and saved.generation_completed_at
                            and saved.saved_at >= saved.generation_completed_at
                            else None,
                            "memory_before": memory_before,
                            "memory_after": backend.json("/system_stats").get("devices"),
                            "images": [f"runs/{saved.run_id}/{name}" for name in saved.images],
                            "workflow_sha256": saved.workflow_sha256,
                        }
                    )
                    save()
                    print(
                        json.dumps({"batch_size": candidates, "completed_request": index + 1}),
                        flush=True,
                    )
            except KomaquillError as error:
                group["failure"] = {
                    "code": str(error.code),
                    "stage": str(error.stage),
                    "run_id": error.details.get("run_id"),
                }
                save()
                if candidates != 4:
                    raise
            finally:
                group["elapsed_seconds"] = time.monotonic() - started
                save()
    report["render_started_at"] = datetime.now(UTC).isoformat()
    with (folder / "story-events.jsonl").open("w", encoding="utf-8") as events:
        for event in api.stories.render(
            story_id, image_profile="klein-4b", seed=31415, candidates=2
        ):
            events.write(event.model_dump_json() + "\n")
            events.flush()
            report["render_id"] = str(event.snapshot.render_id)
            report["story_complete"] = event.snapshot.complete
            save()
            print(json.dumps({"story_states": event.snapshot.counts}), flush=True)
    assert report["story_complete"]
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["phase"] = "awaiting_browser_acceptance_and_explicit_stop"
    save()
    print(str(path), flush=True)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--story-id", type=UUID, required=True)
    parser.add_argument("--allow-gpu", action="store_true", required=True)
    args = parser.parse_args()
    measure(args.story_id)
