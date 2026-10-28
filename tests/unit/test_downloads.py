"""Verify shared model acquisition without external network access."""

import hashlib
from contextlib import contextmanager

import httpx
import pytest

from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure import asset_download
from komaquill.infrastructure.asset_store import model_path
from komaquill.infrastructure.configuration import GpuProject
from komaquill.infrastructure.downloads import fetch_model
from tests.support.presets import asset


def test_verified_model_is_reused_without_download(project, monkeypatch):
    project = GpuProject(project.root, project.settings())
    content = b"verified model"
    model = asset().model_copy(update={"sha256": hashlib.sha256(content).hexdigest()})
    target = model_path(project.models, model)
    target.parent.mkdir(parents=True)
    target.write_bytes(content)

    def forbidden(*args, **kwargs):
        raise AssertionError("The verified file must be reused.")

    monkeypatch.setattr("komaquill.infrastructure.downloads.download_asset", forbidden)
    assert fetch_model(project, model) == target


@pytest.mark.parametrize("case", ["valid", "invalid", "http"])
def test_download_verifies_bytes_before_publication_and_redacts_errors(project, monkeypatch, case):
    project = GpuProject(project.root, project.settings())
    content = b"verified upstream model"
    model = asset().model_copy(
        update={
            "url": "https://example.test/model.safetensors",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    )
    target = model_path(project.models, model)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")

    @contextmanager
    def stream(*args, **kwargs):
        if case == "http":
            raise httpx.ConnectError("private response")
        yield httpx.Response(
            200,
            content=content if case == "valid" else b"invalid",
            request=httpx.Request("GET", model.url),
        )

    monkeypatch.setattr(asset_download.httpx, "stream", stream)
    if case == "valid":
        assert fetch_model(project, model).read_bytes() == content
    else:
        with pytest.raises(KomaquillError) as error:
            fetch_model(project, model)
        assert "private response" not in str(error.value)
        assert target.read_bytes() == b"corrupt"
    assert not target.with_suffix(target.suffix + ".part").exists()


def test_pc_cannot_acquire_model_bodies(project):
    with pytest.raises(KomaquillError) as error:
        fetch_model(project, asset())
    assert error.value.code == "unsupported_asset"
    assert not project.models.exists()


def test_gpu_provider_credentials_are_headers_only(project, monkeypatch):
    project = GpuProject(project.root, project.settings())
    content = b"authenticated weights"
    model = asset("klein-encoder").model_copy(
        update={"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
    )
    observed = []

    @contextmanager
    def stream(method, url, **kwargs):
        observed.append((url, kwargs))
        yield httpx.Response(200, content=content, request=httpx.Request(method, url))

    monkeypatch.setattr(asset_download.httpx, "stream", stream)
    result = fetch_model(project, model, {"huggingface.co": "test-secret"})
    assert result.read_bytes() == content
    assert observed[0][1]["headers"]["Authorization"] == "Bearer test-secret"
    assert "test-secret" not in observed[0][0]
    assert not list(project.root.rglob("token"))


def test_preflight_uses_streaming_get_for_sources_rejecting_head(monkeypatch):
    from komaquill.infrastructure import model_auth

    observed = []
    monkeypatch.setattr(model_auth, "credentials", lambda: {})

    @contextmanager
    def stream(method, url, **kwargs):
        observed.append((method, kwargs))
        yield httpx.Response(
            206, headers={"Content-Type": "binary/octet-stream"}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr(model_auth.httpx, "stream", stream)
    model_auth.preflight([asset()], 1)
    assert observed[0][0] == "GET"
    assert observed[0][1]["headers"]["Range"] == "bytes=0-0"


@pytest.mark.parametrize(
    "status,content_type,expected",
    [
        (401, "application/json", "authentication_failed"),
        (403, "text/plain", "authentication_failed"),
        (200, "text/html", "authentication_failed"),
        (503, "text/plain", "http_failed"),
    ],
)
def test_preflight_classifies_safe_failures(monkeypatch, status, content_type, expected):
    from komaquill.infrastructure import model_auth

    monkeypatch.setattr(model_auth, "credentials", lambda: {})

    @contextmanager
    def stream(method, url, **kwargs):
        yield httpx.Response(
            status, headers={"Content-Type": content_type}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr(model_auth.httpx, "stream", stream)
    with pytest.raises(KomaquillError) as error:
        model_auth.preflight([asset()], 1)
    assert error.value.code == expected


def test_interrupted_transfer_resumes_from_the_written_offset(project, monkeypatch):
    content = b"abcdef"
    model = asset().model_copy(
        update={"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
    )
    calls = []
    failures = []

    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b"ab"
            raise httpx.ReadError("private transport details")

    @contextmanager
    def stream(method, url, **kwargs):
        calls.append(kwargs["headers"])
        yield (
            httpx.Response(200, stream=Interrupted(), request=httpx.Request(method, url))
            if len(calls) == 1
            else httpx.Response(
                206,
                content=b"cdef",
                headers={"Content-Range": "bytes 2-5/6"},
                request=httpx.Request(method, url),
            )
        )

    monkeypatch.setattr(asset_download.httpx, "stream", stream)
    monkeypatch.setattr(asset_download, "CHUNK_BYTES", 2)
    monkeypatch.setattr(asset_download, "DOWNLOAD_RETRY_DELAY_SECONDS", 0)
    result = asset_download.download_asset(
        project.models, model, 1, lambda: False, on_retry=failures.append
    )
    assert result.read_bytes() == content
    assert len(calls) == 2 and calls[1]["Range"] == "bytes=2-"
    assert failures[0].exception_type == "ReadError"
    assert "private transport" not in failures[0].model_dump_json()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_non_transient_download_failure_is_not_retried(project, monkeypatch, status):
    calls = []

    @contextmanager
    def stream(method, url, **kwargs):
        calls.append(True)
        yield httpx.Response(status, request=httpx.Request(method, url))

    monkeypatch.setattr(asset_download.httpx, "stream", stream)
    with pytest.raises(KomaquillError):
        asset_download.download_asset(project.models, asset(), 1, lambda: False)
    assert len(calls) == 1


def test_a_complete_partial_file_is_verified_without_another_get(project, monkeypatch):
    content = b"completed transfer"
    model = asset().model_copy(
        update={"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
    )
    path = model_path(project.models, model)
    path.parent.mkdir(parents=True)
    path.with_suffix(path.suffix + ".part").write_bytes(content)
    monkeypatch.setattr(
        asset_download.httpx, "stream", lambda *a, **kw: pytest.fail("No second GET is needed.")
    )
    assert (
        asset_download.download_asset(project.models, model, 1, lambda: False).read_bytes()
        == content
    )


def test_reused_model_event_identifies_content_and_gpu(project, monkeypatch):
    import json
    from uuid import uuid4

    from komaquill.infrastructure.paths import GpuPaths

    gpu = GpuProject(project.root, project.settings())
    content = b"verified model"
    model = asset().model_copy(update={"sha256": hashlib.sha256(content).hexdigest()})
    target = model_path(gpu.models, model)
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    runtime = uuid4()
    monkeypatch.setenv("KOMAQUILL_RUNTIME_ID", str(runtime))
    fetch_model(gpu, model)
    event = json.loads((GpuPaths(gpu.root).logs / "cli.jsonl").read_bytes())
    assert event["runtime_id"] == str(runtime) and event["sha256"] == model.sha256
