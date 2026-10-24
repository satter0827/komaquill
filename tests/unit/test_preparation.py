"""Test local coordination and CLI confirmation without touching provider APIs."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from komaquill.domain.assets import Preparation
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure import preparation as module
from tests.unit.test_assets import asset


class Client:
    def __init__(self, workspace=None):
        self.job = Preparation(workspace_id=workspace or uuid4(), workflow={"nodes": []})
        self.calls = []

    def json(self, path):
        if path.endswith("capabilities"):
            return {"protocol_version": 1}
        if path == "/object_info":
            return {}
        return {"requests": [self.job.model_dump(mode="json")]}

    def request(self, method, path, **kwargs):
        data = kwargs.get("json", {})
        self.calls.append((path, data))
        if path.endswith("monitor"):
            self.job.workspace_id = (
                uuid4() if data.get("workspace_id") is None else UUID(data["workspace_id"])
            )
        action = data.get("action")
        if action == "plan":
            self.job.phase = "confirmation"
            self.job.assets = [asset()]
        if action == "local_saved":
            self.job.local_saved = True
        if action == "failure":
            self.job.phase = "failed"
        return httpx.Response(200, json=self.job.model_dump(mode="json"))


@pytest.fixture
def coordinator(project, monkeypatch):
    monkeypatch.setattr(module.keyring, "get_password", lambda *a: "a" * 40)
    client = Client()
    control = module.Coordinator(project, client)
    yield control, client
    control.close()


@pytest.mark.parametrize("case", ["supported", "bare", "failed"])
def test_capability_detection_does_not_change_standard_external_access(case):
    def query(path):
        if case != "supported":
            raise KomaquillError(code="http_failed", http_status=404 if case == "bare" else 500)
        return {"protocol_version": 1}

    if case == "failed":
        with pytest.raises(KomaquillError):
            module.capabilities(SimpleNamespace(json=query))
    else:
        assert module.capabilities(SimpleNamespace(json=query)) is (case == "supported")


def test_coordinator_creates_control_credentials_once_and_reuses_workspace(project, monkeypatch):
    stored = []
    monkeypatch.setattr(module.keyring, "get_password", lambda *a: None)
    monkeypatch.setattr(module.keyring, "set_password", lambda *a: stored.append(a))
    first = module.Coordinator(project, Client())
    first.close()
    second = module.Coordinator(project, Client())
    assert first.workspace_id == second.workspace_id and stored
    assert len(first.headers["X-Komaquill-Control"]) >= 32
    second.close()


@pytest.mark.parametrize(
    "case", ["plan", "unsupported", "download", "failed", "cancel", "report_failed"]
)
def test_preparation_jobs_preserve_local_bytes_and_report_safe_errors(
    coordinator, monkeypatch, case
):
    control, client = coordinator
    client.job.workspace_id = control.workspace_id
    client.job.phase = "requested" if case in {"plan", "unsupported"} else "preparing"
    client.job.assets = [asset()]
    client.job.workflow = {
        "nodes": [
            {
                "type": "Loader",
                "widgets_values": ["model.safetensors"],
                "properties": {
                    "models": [
                        {"name": "model.safetensors", "directory": "checkpoints", "url": "url"}
                    ]
                },
            }
        ]
    }
    monkeypatch.setattr(
        module,
        "requirements",
        lambda *a: ([{"name": "model.safetensors", "directory": "checkpoints", "url": "url"}], []),
    )

    def resolve(*args):
        if case == "unsupported":
            raise KomaquillError(code="unsupported_asset")
        return asset()

    monkeypatch.setattr(module, "resolve_model", resolve)

    def download(*args):
        if case in {"failed", "cancel", "report_failed"}:
            raise OSError("private-exception")

    monkeypatch.setattr(module, "download_asset", download)
    if case == "cancel":
        control.cancelled.set()
    if case == "report_failed":
        monkeypatch.setattr(
            control, "update", lambda *a, **k: (_ for _ in ()).throw(KomaquillError())
        )
    control.run(client.job)
    assert client.job.local_saved is (case == "download")
    if case == "unsupported":
        assert client.calls[-1][1]["unsupported"] == ["model.safetensors"]
    assert "private-exception" not in json.dumps(client.calls)


def test_tick_schedules_only_this_workspace_pending_jobs(coordinator):
    control, client = coordinator
    client.job.workspace_id = control.workspace_id
    client.job.phase = "confirmed"
    control.run = lambda job: None
    control.tick()
    control.futures[client.job.request_id].result(timeout=2)
    client.job.local_saved = True
    control.tick()
    client.job.workspace_id = uuid4()
    control.tick()
    assert len(control.futures) == 1


@pytest.mark.parametrize(
    "case", ["ready", "cancelled", "unsupported", "timeout", "interrupt", "failed", "no_extension"]
)
def test_prepare_command_confirms_and_retries_only_existing_session(project, monkeypatch, case):
    workflow = project.root / "workflow.json"
    workflow.write_text('{"nodes":[]}')
    client = Client()
    job = client.job
    job.assets = [asset().model_copy(update={"size_bytes": 5})]
    job.phase = "confirmation"
    job.unsupported = ["unknown"] if case == "unsupported" else []
    monkeypatch.setattr(module, "capabilities", lambda _: case != "no_extension")

    def request(method, path, **kwargs):
        action = kwargs.get("json", {}).get("action")
        if action == "confirm":
            job.phase = "ready"
        if action == "cancel":
            job.phase = "cancelled"
        return httpx.Response(200, json=job.model_dump(mode="json"))

    client.request = request
    if case == "failed":
        job.phase = "failed"
    ticks = iter([0, 1000])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks) if case == "timeout" else 0)

    def sleep(_):
        if case == "interrupt":
            raise KeyboardInterrupt

    monkeypatch.setattr(module.time, "sleep", sleep)
    if case == "ready":
        output = []
        module.prepare_workflow(project, client, workflow, output.append, lambda _: True)
        assert any("5 bytes" in line for line in output)
    else:
        with pytest.raises((KomaquillError, KeyboardInterrupt)):
            module.prepare_workflow(
                project, client, workflow, lambda _: None, lambda _: case != "cancelled"
            )


def test_bindings_are_scoped_to_the_verified_runtime(coordinator, session):
    from komaquill.infrastructure.atomic import atomic_json

    control, client = coordinator
    atomic_json(control.project.session_file, session.model_dump(mode="json"))
    client.job.phase = "ready"
    client.job.assets = [asset()]
    control.bind(client.job)
    assert control.project.prepared_assets(session)[asset().reference].sha256 == asset().sha256
    control.bind(client.job)
    assert not control.project.prepared_assets(session.model_copy(update={"runtime_id": uuid4()}))


@pytest.mark.parametrize("available", [True, False])
def test_managed_start_requires_a_functioning_extension(monkeypatch, available):
    monkeypatch.setattr(module, "capabilities", lambda _: available)
    if available:
        module.verify_extension(None)
    else:
        with pytest.raises(KomaquillError) as error:
            module.verify_extension(None)
        assert error.value.code == "extension_unavailable" and not error.value.retryable


def test_registered_model_plan_uses_verified_local_capacity(coordinator, monkeypatch):
    from komaquill.infrastructure.asset_store import model_path, register

    control, client = coordinator
    model = asset()
    target = model_path(control.project.models, model)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"model")
    register(control.project.models, model, target)
    monkeypatch.setattr(module, "requirements", lambda *a: ([], [model.reference]))
    control.plan(client.job)
    plan = client.calls[-1][1]
    assert plan["assets"][0]["size_bytes"] == 5
    assert plan["reused"] == [model.reference]


@pytest.mark.parametrize("same_node", [False, True])
def test_same_filenames_bind_to_each_nodes_confirmed_source(coordinator, monkeypatch, same_node):
    control, client = coordinator
    hints = [
        {"node_id": "1", "name": "model.safetensors", "directory": "checkpoints", "url": "first"},
        {
            "node_id": "1" if same_node else "2",
            "name": "model.safetensors",
            "directory": "checkpoints",
            "url": "second",
        },
    ]
    monkeypatch.setattr(module, "requirements", lambda *a: (hints, []))
    first, second = asset(b"first"), asset(b"second")
    monkeypatch.setattr(
        module,
        "resolve_model",
        lambda name, role, url, timeout: first if url == "first" else second,
    )
    control.plan(client.job)
    plan = client.calls[-1][1]
    assert len(plan["assets"]) == 2
    if same_node:
        assert plan["unsupported"] == ["model.safetensors"]
    else:
        assert not plan["unsupported"]
        assert plan["node_models"] == {
            "1": {"model.safetensors": first.reference},
            "2": {"model.safetensors": second.reference},
        }
