"""Exercise the native extension contract without importing ComfyUI or allocating a GPU."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from komaquill.domain.assets import Preparation
from komaquill.domain.errors import KomaquillError
from komaquill.gpu import extension as module
from komaquill.infrastructure.asset_store import model_path, register
from komaquill.infrastructure.storage import atomic_json
from tests.unit.test_assets import asset


def test_native_routes_require_confirmation_and_separate_local_preservation(project, monkeypatch):
    calls = []

    def download(*args):
        calls.append(args)
        path = model_path(args[0], args[1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")
        register(args[0], args[1], path)

    monkeypatch.setattr(module, "download_asset", download)

    async def scenario():
        extension = module.Extension(project.artifacts, project.models, 1)
        app = web.Application()
        app.add_routes(extension.routes())
        async with TestClient(TestServer(app)) as client:
            assert (await (await client.get("/komaquill/v1/capabilities")).json())[
                "protocol_version"
            ] == 1
            assert (
                await client.post("/komaquill/v1/requests", json={"workflow": {}})
            ).status == 409
            await client.get("/komaquill/v1/capabilities?client_id=ui")
            assert extension.ui_clients == {"ui"}
            workspace = str(uuid4())
            token = {"X-Komaquill-Control": "a" * 40}
            assert (
                await client.post("/komaquill/v1/monitor", json={"workspace_id": workspace})
            ).status == 403
            assert (
                await client.post(
                    "/komaquill/v1/monitor", headers=token, json={"workspace_id": workspace}
                )
            ).status == 200
            assert (
                await client.post(
                    "/komaquill/v1/monitor", headers=token, json={"workspace_id": str(uuid4())}
                )
            ).status == 409
            job = await (
                await client.post("/komaquill/v1/requests", json={"workflow": {"nodes": []}})
            ).json()
            route = f"/komaquill/v1/requests/{job['request_id']}"
            plan = {
                "action": "plan",
                "assets": [asset().model_dump(mode="json")],
                "unsupported": [],
                "reused": [],
            }
            assert (await client.post(route, json=plan)).status == 403
            assert (await client.post(route, headers=token, json=plan)).status == 200
            assert not calls
            assert (await client.post(route, json={"action": "invalid"})).status == 409
            assert (await client.post(route, json={"action": "confirm"})).status == 200
            await asyncio.gather(*extension.tasks.values())
            state = extension.read(job["request_id"])
            assert state.remote_ready and not state.local_saved and state.phase == "preparing"
            assert (
                await client.post(route, headers=token, json={"action": "local_saved"})
            ).status == 200
            assert extension.read(job["request_id"]).phase == "ready"
            extension.update(job["request_id"], phase="failed")
            assert extension.read(job["request_id"]).phase == "ready"
            assert (await client.post("/komaquill/v1/requests/not-a-uuid", json={})).status == 400
            assert len((await (await client.get("/komaquill/v1/requests")).json())["requests"]) == 1
            await client.post(
                "/komaquill/v1/monitor",
                headers=token,
                json={"workspace_id": workspace, "accepting": False},
            )
            assert (
                await client.post("/komaquill/v1/requests", json={"workflow": {}})
            ).status == 409

    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize("case", ["cancel", "failure", "local_first", "unsupported", "stop"])
def test_preparation_terminal_states_are_monotonic(project, monkeypatch, case):
    async def scenario():
        extension = module.Extension(project.artifacts, project.models, 1)
        extension.workspace_id = uuid4()
        job = Preparation(
            workspace_id=extension.workspace_id,
            workflow={},
            phase="confirmation",
            assets=[asset()],
            local_saved=case == "local_first",
        )
        atomic_json(extension.path(job.request_id), job.model_dump(mode="json"))

        def download(*args):
            if case == "failure":
                raise KomaquillError(code="http_failed")
            if case == "cancel":
                extension.update(job.request_id, phase="cancelled")
            path = model_path(args[0], args[1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"model")
            register(args[0], args[1], path)

        monkeypatch.setattr(module, "download_asset", download)
        await extension.prepare(job.request_id)
        final = extension.read(job.request_id)
        assert final.phase == {
            "cancel": "cancelled",
            "failure": "failed",
            "local_first": "ready",
        }.get(case, "preparing")
        if case == "cancel":
            extension.update(job.request_id, phase="ready")
            assert extension.read(job.request_id).phase == "cancelled"

    asyncio.run(scenario())


def test_register_uses_existing_promptserver_routes(project, monkeypatch):
    monkeypatch.setattr(
        module.importlib,
        "import_module",
        lambda _: SimpleNamespace(
            add_model_folder_path=lambda *a: None,
            get_input_directory=lambda: str(project.artifacts / "input"),
        ),
    )
    server = SimpleNamespace(
        routes=web.RouteTableDef(),
        app=web.Application(),
        add_on_prompt_handler=lambda handler: None,
    )
    module.register(server, project.artifacts, 1)
    assert len(server.routes) == 10


@pytest.mark.parametrize(
    "case", ["saved", "hash", "oversize", "unknown", "unauthorized", "cancelled"]
)
def test_local_only_models_stream_to_a_confirmed_digest(project, case):
    async def scenario():
        extension = module.Extension(project.artifacts, project.models, 1)
        extension.control_token = "a" * 40
        local = asset().model_copy(update={"url": None, "local_only": True, "size_bytes": 5})
        job = Preparation(
            workspace_id=uuid4(),
            workflow={},
            phase="cancelled" if case == "cancelled" else "preparing",
            assets=[local],
            local_saved=True,
        )
        atomic_json(extension.path(job.request_id), job.model_dump(mode="json"))
        app = web.Application()
        app.add_routes(extension.routes())
        async with TestClient(TestServer(app)) as client:
            sha = "0" * 64 if case == "unknown" else local.sha256
            response = await client.post(
                f"/komaquill/v1/requests/{job.request_id}/upload/{sha}",
                headers={} if case == "unauthorized" else {"X-Komaquill-Control": "a" * 40},
                data=b"corrupt" if case == "oversize" else b"wrong" if case == "hash" else b"model",
            )
            assert response.status == {
                "saved": 200,
                "unknown": 409,
                "unauthorized": 403,
                "cancelled": 409,
            }.get(case, 400)
            assert model_path(project.models, local).exists() is (case == "saved")
            assert not list(project.artifacts.glob("models/**/*upload"))
            if case == "saved":
                assert extension.read(job.request_id).phase == "ready"

    asyncio.run(scenario())
