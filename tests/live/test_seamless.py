"""Exercise explicit model preparation and native file preservation on an owned runtime."""

import hashlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from komaquill.infrastructure.configuration import find_project
from komaquill.infrastructure.materials import blob, records
from komaquill.infrastructure.runtime import connected
from komaquill.infrastructure.storage import load_runs
from komaquill.infrastructure.workflows import prepare

pytestmark = pytest.mark.skipif(
    os.environ.get("KOMAQUILL_LIVE") != "1", reason="Requires an explicitly started ComfyUI"
)


def wait_for(predicate, timeout=120):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.5)


def test_native_upload_workflow_save_and_immutable_generation():
    project = find_project(Path(os.environ["KOMAQUILL_PROJECT_DIR"]))
    name = "seamless-" + uuid4().hex + ".png"
    png = io.BytesIO()
    Image.new("RGB", (64, 64), "green").save(png, "PNG")
    content = png.getvalue()
    sha = hashlib.sha256(content).hexdigest()
    with connected(project) as (client, session):
        response = client.request(
            "POST",
            "/upload/image",
            files={"image": (name, content, "image/png")},
            data={"type": "input", "overwrite": "true"},
        ).json()
        assert response["name"] == name
        ui = {
            "last_node_id": 2,
            "last_link_id": 1,
            "nodes": [
                {
                    "id": 1,
                    "type": "LoadImage",
                    "pos": [0, 0],
                    "size": [315, 314],
                    "flags": {},
                    "order": 0,
                    "mode": 0,
                    "inputs": [],
                    "outputs": [
                        {"name": "IMAGE", "type": "IMAGE", "links": [1]},
                        {"name": "MASK", "type": "MASK", "links": []},
                    ],
                    "properties": {},
                    "widgets_values": [name, "image"],
                },
                {
                    "id": 2,
                    "type": "SaveImage",
                    "pos": [400, 0],
                    "size": [315, 314],
                    "flags": {},
                    "order": 1,
                    "mode": 0,
                    "inputs": [{"name": "images", "type": "IMAGE", "link": 1}],
                    "outputs": [],
                    "properties": {},
                    "widgets_values": ["seamless-input"],
                },
            ],
            "links": [[1, 1, 0, 2, 0, "IMAGE"]],
            "groups": [],
            "config": {},
            "extra": {},
            "version": 0.4,
        }
        path = "workflows/seamless-input.json"
        from urllib.parse import quote

        client.request(
            "POST", "/userdata/" + quote(path, safe=""), params={"overwrite": "true"}, json=ui
        )
        graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": name}},
            "2": {
                "class_type": "SaveImage",
                "inputs": {"images": ["1", 0], "filename_prefix": "seamless-input"},
            },
        }
        prompt_id = client.submit(graph, ui, str(uuid4()))
        wait_for(
            lambda: any(
                run.prompt_id == prompt_id and run.state == "saved" for run in load_runs(project)
            )
        )
        run = next(run for run in load_runs(project) if run.prompt_id == prompt_id)
        assert sha in run.input_sha256.values()
        wait_for(lambda: any(row.name == path for row in records(project, "workflows")))
        saved = next(row for row in records(project, "workflows") if row.name == path)
        assert saved.input_sha256 == {name: sha}
        assert (
            json.loads(blob(project, saved).read_bytes())["nodes"][0]["widgets_values"][0] == name
        )
        other = io.BytesIO()
        Image.new("RGB", (64, 64), "blue").save(other, "PNG")
        client.request(
            "POST",
            "/upload/image",
            files={"image": (name, other.getvalue(), "image/png")},
            data={"type": "input", "overwrite": "true"},
        )
        wait_for(lambda: len([row for row in records(project, "inputs") if row.name == name]) >= 2)
        assert (
            blob(
                project,
                next(
                    row
                    for row in records(project, "inputs")
                    if row.name == name and row.sha256 == sha
                ),
            ).read_bytes()
            == content
        )


def test_prepare_adds_the_pinned_adapter_without_another_gpu():
    project = find_project(Path(os.environ["KOMAQUILL_PROJECT_DIR"]))
    with connected(project) as (client, session):
        settings = session.settings.model_copy(update={"profiles": ("pony-preview",)})
        graph, ui = prepare(
            "a sunny meadow, landscape",
            None,
            944,
            "seamless-adapter",
            settings=settings,
            profile_name="pony-preview",
        )
        asset = settings.assets["hyper-sdxl"]
        loader = next(node for node in ui["nodes"] if node["id"] == 12)
        loader["widgets_values"][0] = asset.filename
        loader["properties"]["models"] = [
            {
                "name": asset.filename,
                "directory": asset.role,
                "url": f"https://huggingface.co/{asset.repo_id}/resolve/{asset.revision}/{asset.filename}",
            }
        ]
        path = project.artifacts / "tmp/live/adapter.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ui), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", "-m", "komaquill", "prepare", str(path)],
            cwd=project.root,
            input="y\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ready" in result.stdout.lower()
        assert (
            asset.reference
            in client.json("/object_info/LoraLoaderModelOnly")["LoraLoaderModelOnly"]["input"][
                "required"
            ]["lora_name"][0]
        )
        assert client.runtime()["runtime_id"] == str(session.runtime_id)
        prompt_id = client.submit(graph, ui, str(uuid4()))
        wait_for(
            lambda: any(
                run.prompt_id == prompt_id and run.state == "saved" for run in load_runs(project)
            ),
            timeout=300,
        )
        saved = next(run for run in load_runs(project) if run.prompt_id == prompt_id)
        assert saved.asset_sha256[asset.reference] == asset.sha256
