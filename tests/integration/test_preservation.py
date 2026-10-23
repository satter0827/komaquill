"""Verify native write observation and immutable generation inputs without ComfyUI."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from komaquill.gpu.extension import Extension
from komaquill.gpu.preservation import Preservation


def harness(project):
    control = Extension(project.artifacts, project.artifacts, 1)
    control.workspace_id = uuid4()
    control.control_token = "a" * 40
    control.ui_clients.add("ui")
    notifications = []
    app = web.Application()
    server = SimpleNamespace(
        app=app,
        sockets={"ui": object(), "not-subscribed": object()},
        routes=web.RouteTableDef(),
        send_sync=lambda *a: notifications.append(a),
        add_on_prompt_handler=lambda handler: None,
        user_manager=SimpleNamespace(
            get_request_user_filepath=lambda request, name, **kw: str(
                project.artifacts / "userdata" / name
            )
        ),
    )
    preservation = Preservation(server, control, project.artifacts / "input")
    control.preservation = preservation
    return control, preservation, server, notifications


def test_queued_inputs_keep_their_original_content_after_overwrite(project):
    control, files, server, notifications = harness(project)
    source = files.input_root / "same.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"first image")
    data = {
        "prompt": {
            "1": {"class_type": "LoadImage", "inputs": {"image": "same.png"}},
            "2": {"class_type": "KSampler"},
        },
        "extra_data": {"komaquill": {"run_id": str(uuid4())}},
    }
    result = files.pin_inputs(data)
    reference = result["prompt"]["1"]["inputs"]["image"]
    source.write_bytes(b"replacement")
    assert (files.input_root / reference).read_bytes() == b"first image"
    assert (
        result["extra_data"]["komaquill"]["input_sha256"][reference]
        == hashlib.sha256(b"first image").hexdigest()
    )
    assert files.rows()[0].sha256 == hashlib.sha256(b"first image").hexdigest()
    assert not notifications


@pytest.mark.parametrize("case", ["inactive", "linked", "escape", "missing"])
def test_input_pinning_fails_closed_or_leaves_linked_inputs_untouched(project, case):
    control, files, server, notifications = harness(project)
    if case == "inactive":
        control.workspace_id = None
    value = (
        ["from-node", 0]
        if case == "linked"
        else "../escape.png"
        if case == "escape"
        else "missing.png"
    )
    data = {"prompt": {"1": {"class_type": "LoadImageMask", "inputs": {"image": value}}}}
    result = files.pin_inputs(data)
    assert ("prompt" in result) is (case in {"inactive", "linked"})
    assert bool(notifications) is (case in {"escape", "missing"})


def test_native_upload_and_save_observation_preserves_each_revision_and_acknowledges_local_save(
    project,
):
    async def scenario():
        control, files, server, notifications = harness(project)

        async def upload(request):
            post = await request.post()
            name = post.get("name", "same.png")
            source = files.input_root / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(post["content"].encode())
            return web.json_response(
                {"name": name, "subfolder": "", "type": post.get("type", "input")}
            )

        async def save(request):
            name = request.match_info["file"]
            source = project.artifacts / "userdata" / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(await request.read())
            return web.json_response(name)

        async def failed(request):
            return web.Response(status=400)

        server.routes.post("/upload/image")(upload)
        server.routes.post("/upload/mask")(upload)
        server.routes.post("/userdata/{file:.*}")(save)

        async def queue(request):
            return web.json_response({"queued": True})

        server.routes.post("/prompt")(queue)
        server.routes.post("/untouched")(failed)
        files.install()
        server.app.add_routes(server.routes)
        server.app.add_routes(control.routes())
        async with TestClient(TestServer(server.app)) as client:
            for content in ("first", "second"):
                assert (await client.post("/upload/image", data={"content": content})).status == 200
            await client.post("/upload/mask", data={"content": "mask"})
            await client.post("/upload/image", data={"content": "temp", "type": "temp"})
            await client.post("/upload/image", data={"content": "unsupported", "name": "movie.mp4"})
            await client.post("/userdata/workflows/test.json", json={"nodes": []})
            await client.post("/userdata/settings.json", json={})
            assert (await client.post("/untouched")).status == 400
            index = await (await client.get("/komaquill/v1/files")).json()
            assert len(index["files"]) == 4
            first = index["files"][0]
            route = "/komaquill/v1/files/" + first["material_id"]
            response = await client.get(route)
            assert hashlib.sha256(await response.read()).hexdigest() == first["sha256"]
            assert (await client.post(route, json={"sha256": first["sha256"]})).status == 403
            token = {"X-Komaquill-Control": "a" * 40}
            assert (
                await client.post(route, headers=token, json={"sha256": "0" * 64})
            ).status == 409
            assert (
                await client.post(route, headers=token, json={"sha256": first["sha256"]})
            ).status == 200
            assert notifications[-1][1]["state"] == "saved"
            assert (
                await client.post("/komaquill/v1/save-status", json={"state": "saved"})
            ).status == 403
            assert (
                await client.post(
                    "/komaquill/v1/save-status", headers=token, json={"state": "optimistic"}
                )
            ).status == 400
            assert (
                await client.post(
                    "/komaquill/v1/save-status",
                    headers=token,
                    json={"state": "saved", "prompt_id": "prompt"},
                )
            ).status == 200
            control.accepting = False
            assert (await client.post("/prompt")).status == 409
            assert (await client.post("/upload/image", data={"content": "late"})).status == 409

    asyncio.run(scenario())


def test_saved_workflow_captures_the_input_revision_and_corrupt_records_are_reported(project):
    control, files, server, notifications = harness(project)
    source = files.input_root / "input.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original")
    workflow = project.root / "workflow.json"
    workflow.write_text(
        json.dumps(
            {
                "nodes": [
                    {"type": "LoadImage", "widgets_values": ["input.png"]},
                    {"type": "KSampler"},
                ]
            }
        )
    )
    captured = files.snapshot(workflow, "workflows", "workflows/test.json")
    source.write_bytes(b"replacement")
    assert captured.input_sha256 == {"input.png": hashlib.sha256(b"original").hexdigest()}
    broken_id = uuid4()
    (files.folder / f"{broken_id}.json").write_text("{broken")
    assert len(files.rows()) == 2
    assert files.errors == [str(broken_id)]


def test_notifications_are_opt_in_and_do_not_target_loading_or_disconnected_clients(project):
    control, files, server, notifications = harness(project)
    files.emit({"state": "saving"})
    assert notifications == [("komaquill_saved", {"state": "saving"}, "ui")]
    server.sockets.clear()
    files.emit({"state": "saved"})
    assert len(notifications) == 1 and not control.ui_clients
