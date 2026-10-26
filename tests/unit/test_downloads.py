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
    source = tmp_path / "download.safetensors"
    source.write_bytes(content)
    observed = []

    def download(**kwargs):
        observed.append(kwargs)
        return str(source)

    monkeypatch.setattr("huggingface_hub.hf_hub_download", download)
    assert fetch_model(project, model).read_bytes() == content
    assert observed[0]["revision"] == model.url.split("/resolve/")[1].split("/")[0]
    assert observed[0]["filename"].endswith("/model.safetensors")
    assert "token" not in observed[0]


def test_gpu_refuses_missing_transferred_assets_without_network(project):
    with pytest.raises(KomaquillError) as error:
        fetch_model(GpuProject(project.root, project.settings()), asset("klein-encoder"))
    assert error.value.code == "invalid_output"
