"""Verify shutdown against an explicitly started, owned Colab runtime."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

from komaquill.infrastructure.colab import Colab, check_port
from komaquill.infrastructure.configuration import find_project
from komaquill.infrastructure.runtime import connected
from komaquill.infrastructure.storage import load_runs
from komaquill.infrastructure.workflows import prepare


@pytest.mark.parametrize("mode", ["normal", "recovery-force"])
def test_live_owned_shutdown(mode):
    if os.environ.get("KOMAQUILL_LIVE_STOP") != mode:
        pytest.skip("Select the owned runtime shutdown scenario explicitly")
    project = find_project(Path(os.environ["KOMAQUILL_PROJECT_DIR"]))
    control = Colab(project)
    receipt = control.receipt()
    assert receipt is not None and control.refresh(receipt)
    with connected(project) as (client, session):
        assert receipt.runtime_id == session.runtime_id
        graph, ui = prepare(
            "a cabin in a mountain valley, watercolor", "text, watermark", 834, str(uuid4())
        )
        steps = 200 if mode == "normal" else 10000
        graph["3"]["inputs"]["steps"] = steps
        next(node for node in ui["nodes"] if node["id"] == 3)["widgets_values"][2] = steps
        identity = client.submit(graph, ui, str(uuid4()))
        if mode == "recovery-force":
            deadline = time.monotonic() + 30
            while not any(run.prompt_id == identity for run in load_runs(project)):
                assert time.monotonic() < deadline
                time.sleep(0.1)
            monitors = []
            for process in psutil.process_iter(["exe", "cmdline"]):
                args = process.info["cmdline"] or []
                executable = Path(process.info["exe"] or ".")
                if executable.is_relative_to(project.runtime) and args[-3:] == [
                    "-m",
                    "komaquill",
                    "start",
                ]:
                    monitors.append(process)
            assert monitors, "The explicitly started project monitor was not found"
            for process in monitors:
                process.terminate()
            _, alive = psutil.wait_procs(monitors, timeout=10)
            assert not alive
            control.cleanup_processes(receipt)
            assert control.refresh(receipt), "The remote runtime must survive local disconnection"
    args = [sys.executable, "-B", "-m", "komaquill", "stop"]
    if mode == "recovery-force":
        args.append("--force")
    result = subprocess.run(
        args, cwd=project.root, capture_output=True, text=True, encoding="utf-8", timeout=240
    )
    assert result.returncode == 0, result.stdout + result.stderr
    folder = project.artifacts / "logs" / str(receipt.runtime_id)
    summary = json.loads((folder / "shutdown.json").read_bytes())
    assert summary["gpu"] == "stopped" and summary["logs"] == "ok"
    assert summary["connections"] == "ok" and summary["history"] == "ok"
    assert not summary["errors"] and control.receipt() is None
    run = next(run for run in load_runs(project) if run.prompt_id == identity)
    assert run.state == ("saved" if mode == "normal" else "lost")
    if mode == "recovery-force":
        assert summary["force"] and summary["collection"] == "incomplete"
        assert summary["lost_ids"] == [str(run.run_id)] and "Lost: 1" in result.stdout
    else:
        assert run.image_metadata and summary["collection"] == "ok"
    assert not json.loads((folder / "capture.json").read_bytes())["failed"]
    check_port(session.settings.local_port)
