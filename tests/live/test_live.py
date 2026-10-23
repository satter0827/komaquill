"""Verify a running managed ComfyUI without allocating a GPU."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from websockets.sync.client import connect

from komaquill.infrastructure.configuration import find_project
from komaquill.infrastructure.runtime import connected, drain
from komaquill.infrastructure.storage import load_runs
from komaquill.infrastructure.workflows import prepare

pytestmark = pytest.mark.skipif(
    os.environ.get("KOMAQUILL_LIVE") != "1", reason="Requires an explicitly started ComfyUI"
)


def test_live_websocket_and_saved_integrity():
    project = find_project(Path(os.environ["KOMAQUILL_PROJECT_DIR"]))
    with connected(project) as (client, session):
        url = session.endpoint.replace("http://", "ws://").replace("https://", "wss://")
        with connect(url + "/ws?clientId=" + str(uuid4()), open_timeout=10) as socket:
            message = json.loads(socket.recv(timeout=10))
            assert message["type"] == "status"
        assert client.json("/system_stats")["devices"]
        runs = [run for run in load_runs(project) if run.runtime_id == session.runtime_id]
        assert any(run.state == "saved" and run.image_metadata for run in runs)


def test_live_normal_wait_collects_a_browser_request_without_stopping_gpu():
    project = find_project(Path(os.environ["KOMAQUILL_PROJECT_DIR"]))
    with connected(project) as (client, session):
        graph, ui = prepare(
            "a sunny forest stream, watercolor", "text, watermark", 833, str(uuid4())
        )
        graph["3"]["inputs"]["steps"] = 100
        node = next(node for node in ui["nodes"] if node["id"] == 3)
        node["widgets_values"][2] = 100
        identity = client.submit(graph, ui, str(uuid4()))
        result = drain(project, client, session, False, lambda _: None)
        assert not result.errors and not result.pending_ids
        runs = [run for run in load_runs(project) if run.prompt_id == identity]
        assert len(runs) == 1 and runs[0].state == "saved"
        assert client.json("/system_stats")["devices"]
