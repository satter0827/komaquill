"""Test material preservation, partial recovery and restoration of exact file revisions."""

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from komaquill.domain.errors import KomaquillError
from komaquill.domain.materials import Material
from komaquill.infrastructure import materials as module
from komaquill.infrastructure.storage import atomic_bytes, atomic_json


def material(content=b"image", **changes):
    values = {
        "workspace_id": uuid4(),
        "kind": "inputs",
        "name": "source.png",
        "reference": "source.png",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    return Material.model_validate({**values, **changes})


@pytest.mark.parametrize(
    "case", ["healthy", "partial", "hash", "size", "storage", "ack", "invalid", "missing_blob"]
)
def test_each_file_recovers_independently_and_acknowledges_after_durable_save(
    project, session, monkeypatch, case
):
    rows = [material(), material(name="other.png")]
    atomic_json(project.session_file, session.model_dump(mode="json"))
    calls = []
    broken = {rows[0].material_id}

    def request(method, path, **kw):
        identity = path.rsplit("/", 1)[-1]
        calls.append((method, identity))
        if (
            identity == str(rows[0].material_id)
            and broken
            and case in {"partial", "ack"}
            and method == ("POST" if case == "ack" else "GET")
        ):
            raise KomaquillError(code="connection_failed")
        if method == "POST":
            assert (project.inputs / "records" / f"{identity}.json").is_file()
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            content=b"bad"
            if case in {"hash", "size"} and identity == str(rows[0].material_id)
            else b"image",
        )

    if case == "size":
        rows[0].size_bytes = 99
    client = SimpleNamespace(
        json=lambda _: {
            "files": [r.model_dump(mode="json") for r in rows] + ([{}] if case == "invalid" else [])
        },
        request=request,
    )
    collector = module.MaterialCollector(project, client, {"control": "test"})
    if case == "storage":
        from komaquill.infrastructure import artifacts

        original = artifacts.atomic_bytes

        def fail(path, content):
            if not broken:
                return original(path, content)
            broken.clear()
            raise OSError("private-error")

        monkeypatch.setattr(artifacts, "atomic_bytes", fail)
    errors = collector.collect()
    assert bool(errors) is (case not in {"healthy", "missing_blob"})
    assert len(module.records(project, "inputs")) >= 1
    broken.clear()
    if case not in {"hash", "size", "invalid"}:
        assert not collector.collect()
    if case == "missing_blob":
        module.blob(project, rows[0]).unlink()
        assert not collector.collect()
        assert module.blob(project, rows[0]).read_bytes() == b"image"
    assert all("private-error" not in p.read_text() for p in (project.logs).glob("**/*.jsonl"))


@pytest.mark.parametrize("case", ["credential", "missing"])
def test_control_credentials_are_read_without_exposure(project, monkeypatch, case):
    monkeypatch.setattr(
        module.keyring, "get_password", lambda *a: "test-control" if case == "credential" else None
    )
    if case == "credential":
        assert (
            module.MaterialCollector(project, None).headers["X-Komaquill-Control"] == "test-control"
        )
    else:
        with pytest.raises(KomaquillError):
            module.MaterialCollector(project, None)


def store(project, row, content):
    row.state = "saved"
    atomic_bytes(module.blob(project, row), content)
    atomic_json(
        project.data / row.kind / "records" / f"{row.material_id}.json",
        row.model_dump(mode="json"),
    )


def test_restore_uses_saved_workflow_inputs_instead_of_latest_overwritten_names(project):
    first = material()
    second = material(b"new input")
    store(project, first, b"image")
    store(project, second, b"new input")
    content = json.dumps(
        {"nodes": [{"type": "LoadImage", "widgets_values": ["source.png"]}, {"type": "KSampler"}]}
    ).encode()
    workflow = material(
        content,
        kind="workflows",
        name="workflows/test.json",
        reference="workflows/test.json",
        input_sha256={"source.png": first.sha256},
    )
    store(project, workflow, content)
    calls = []

    def request(method, path, **kwargs):
        calls.append((path, kwargs))
        if path == "/upload/image":
            assert kwargs["files"]["image"][1].read() == b"image"
            return httpx.Response(200, json={"name": "source.png", "subfolder": "restored"})
        assert kwargs["json"]["nodes"][0]["widgets_values"][0] == "restored/source.png"
        return httpx.Response(200)

    module.MaterialCollector(project, SimpleNamespace(request=request), {}).restore()
    assert len(calls) == 2
    assert len(module.records(project, "inputs")) == 2
    assert module.blob(project, workflow).read_bytes() == content


def test_missing_required_input_is_reported_without_uploading_workflow(project):
    content = b'{"nodes":[{"type":"LoadImageMask","widgets_values":["missing.png"]}]}'
    row = material(content, kind="workflows", name="workflows/test.json")
    store(project, row, content)
    with pytest.raises(KomaquillError):
        module.MaterialCollector(project, None, {}).restore()


def test_only_confirmed_owned_runtime_marks_unpreserved_files_lost(project, session):
    pending = material(runtime_id=session.runtime_id)
    saved = material(runtime_id=session.runtime_id, state="saved")
    other = material(runtime_id=uuid4())
    for row in [pending, saved, other]:
        atomic_json(
            project.state / "preservation" / f"{row.material_id}.json", row.model_dump(mode="json")
        )
    assert module.finalize_materials(project, session.runtime_id) == (str(pending.material_id),)
    assert (
        Material.model_validate_json(
            (project.state / "preservation" / f"{other.material_id}.json").read_bytes()
        ).state
        == "pending"
    )


@pytest.mark.parametrize(
    "reference", ["../escape.png", "/absolute.png", "C:/input.png", "bad\\input.png"]
)
def test_material_references_cannot_redirect_storage(reference):
    with pytest.raises(ValueError):
        material(name=reference)


def test_save_notifications_wait_for_required_inputs_and_ignore_other_runtimes(project, session):
    from komaquill.application.generation import new_run
    from komaquill.infrastructure.storage import save_run

    atomic_json(project.session_file, session.model_dump(mode="json"))
    current = new_run(project, session, uuid4(), {})
    current.state = "saved"
    current.prompt_id = "current"
    current.input_sha256 = {"source.png": material().sha256}
    save_run(project, current)
    other = new_run(project, session.model_copy(update={"runtime_id": uuid4()}), uuid4(), {})
    other.prompt_id = "other"
    save_run(project, other)
    calls = []
    client = SimpleNamespace(
        request=lambda *a, **kw: calls.append(kw["json"]) or httpx.Response(200)
    )
    collector = module.MaterialCollector(project, client, {})
    collector.notify_runs()
    assert calls == [{"state": "retry", "prompt_id": "current"}]
    store(project, material(), b"image")
    collector.notify_runs()
    collector.notify_runs()
    assert calls[-1] == {"state": "saved", "prompt_id": "current"} and len(calls) == 2


def test_material_index_corruption_does_not_hide_valid_files(project):
    row = material()
    client = SimpleNamespace(
        json=lambda _: {
            "files": [{}, row.model_dump(mode="json")],
            "errors": ["unreadable-remote-record"],
        },
        request=lambda method, path, **kw: httpx.Response(200, content=b"image"),
    )
    errors = module.MaterialCollector(project, client, {}).collect()
    assert errors == ["unreadable-remote-record", "material-index:0"]
    assert module.blob(project, row).is_file()


def test_finalization_during_download_never_acknowledges_or_reopens_lost_material(project, session):
    from filelock import FileLock

    row = Material(
        workspace_id=uuid4(),
        runtime_id=session.runtime_id,
        kind="inputs",
        name="late.png",
        reference="late.png",
        sha256=hashlib.sha256(b"image").hexdigest(),
        size_bytes=5,
    )
    acknowledgements = []

    def request(method, path, **kwargs):
        if method == "GET":
            with FileLock(str(project.state / ".materials.lock"), timeout=0):
                pass
            assert module.finalize_materials(project, session.runtime_id) == (str(row.material_id),)
            return httpx.Response(200, content=b"image")
        acknowledgements.append(path)
        return httpx.Response(200)

    client = SimpleNamespace(
        json=lambda _: {"files": [row.model_dump(mode="json")]}, request=request
    )
    assert module.MaterialCollector(project, client, {}).collect() == []
    operational = project.state / "preservation" / f"{row.material_id}.json"
    assert Material.model_validate_json(operational.read_bytes()).state == "lost"
    assert not acknowledgements and not module.records(project, "inputs")
