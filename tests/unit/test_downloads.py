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


def test_gated_hub_download_uses_standard_sdk_and_verifies_hash(project, monkeypatch, tmp_path):
    content = b"encoder weights"
    model = asset("klein-encoder").model_copy(
        update={
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    )
    observed = []
    file_id = []

    def download(**kwargs):
        observed.append(kwargs)
        source = kwargs["local_dir"] / kwargs["filename"]
        source.parent.mkdir(parents=True)
        source.write_bytes(content)
        file_id.append(source.stat().st_ino)
        return str(source)

    monkeypatch.setattr("huggingface_hub.hf_hub_download", download)
    result = fetch_model(project, model)
    assert result.read_bytes() == content
    assert result.stat().st_ino == file_id[0]
    assert not (result.parent / ".hub").exists()
    assert "cache_dir" not in observed[0]
    assert observed[0]["revision"] == model.url.split("/resolve/")[1].split("/")[0]
    assert observed[0]["filename"].endswith("/model.safetensors")
    assert "token" not in observed[0]


def test_gated_download_cancellation_keeps_the_sdk_file_for_resume(project, monkeypatch):
    content = b"resumable encoder"
    model = asset("klein-encoder").model_copy(
        update={"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
    )
    stopped = False
    downloaded = []

    def download(**kwargs):
        nonlocal stopped
        path = kwargs["local_dir"] / kwargs["filename"]
        if not path.exists():
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
            downloaded.append(path.stat().st_ino)
            stopped = True
        return str(path)

    monkeypatch.setattr("huggingface_hub.hf_hub_download", download)
    with pytest.raises(KomaquillError) as error:
        asset_download.download_asset(project.models, model, 1, lambda: stopped)
    assert error.value.code == "stopping"
    assert not model_path(project.models, model).exists()
    stopped = False
    result = asset_download.download_asset(project.models, model, 1, lambda: stopped)
    assert result.stat().st_ino == downloaded[0]
    assert len(downloaded) == 1


def test_gated_download_rejects_corruption_without_replacing_the_existing_file(
    project, monkeypatch
):
    model = asset("klein-encoder")
    target = model_path(project.models, model)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous file")

    def download(**kwargs):
        path = kwargs["local_dir"] / kwargs["filename"]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"corrupt download")
        return str(path)

    monkeypatch.setattr("huggingface_hub.hf_hub_download", download)
    with pytest.raises(KomaquillError) as error:
        fetch_model(project, model)
    assert error.value.code == "invalid_output"
    assert target.read_bytes() == b"previous file"
    assert not list((target.parent / ".hub").rglob("*.safetensors"))


def test_gpu_refuses_missing_transferred_assets_without_network(project):
    with pytest.raises(KomaquillError) as error:
        fetch_model(GpuProject(project.root, project.settings()), asset("klein-encoder"))
    assert error.value.code == "invalid_output"


def test_gated_download_cannot_publish_a_file_outside_its_staging_folder(
    project, monkeypatch, tmp_path
):
    foreign = tmp_path / "foreign.safetensors"
    foreign.write_bytes(b"unrelated content")
    monkeypatch.setattr("huggingface_hub.hf_hub_download", lambda **kwargs: str(foreign))
    with pytest.raises(KomaquillError) as error:
        fetch_model(project, asset("klein-encoder"))
    assert error.value.code == "invalid_output"
    assert foreign.read_bytes() == b"unrelated content"
