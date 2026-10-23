"""Test cached model reuse and replacement after corruption."""

import hashlib

import pytest

from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.downloads import fetch_model
from komaquill.infrastructure.workflows import preset


def test_verified_model_is_reused_without_network(project, monkeypatch):
    import huggingface_hub

    content = b"verified test model"
    model = preset().model_copy(update={"sha256": hashlib.sha256(content).hexdigest()})
    target = project.artifacts / "models" / model.name / model.filename
    target.parent.mkdir(parents=True)
    target.write_bytes(content)

    def forbidden(**_kwargs):
        raise AssertionError("ネットワークへ接続しないこと")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", forbidden)
    assert fetch_model(project.artifacts, model) == target


def test_corrupted_model_is_downloaded_again(project, monkeypatch):
    import huggingface_hub

    content = b"verified test model"
    model = preset().model_copy(update={"sha256": hashlib.sha256(content).hexdigest()})
    target = project.artifacts / "models" / model.name / model.filename
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupted")

    def download(**kwargs):
        assert kwargs["force_download"] is True
        target.write_bytes(content)
        return str(target)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    assert fetch_model(project.artifacts, model).read_bytes() == content


@pytest.mark.parametrize("case", ["valid", "invalid"])
def test_url_asset_is_verified_before_replacing_target(project, monkeypatch, case):
    from contextlib import contextmanager

    import httpx

    from komaquill.infrastructure import downloads as module

    content = b"verified upstream model"
    model = preset().model_copy(
        update={
            "name": "upscaler",
            "repo_id": "",
            "revision": "",
            "url": "https://example.test/model.pth",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    )

    @contextmanager
    def stream(*args, **kwargs):
        yield httpx.Response(
            200,
            content=content if case == "valid" else b"corrupt",
            request=httpx.Request("GET", model.url),
        )

    monkeypatch.setattr(module.httpx, "stream", stream)
    if case == "valid":
        assert fetch_model(project.artifacts, model).read_bytes() == content
    else:
        with pytest.raises(KomaquillError):
            fetch_model(project.artifacts, model)
        assert not (project.artifacts / "models/upscaler" / model.filename).is_file()
    assert not list((project.artifacts / "models/upscaler").glob("*.part"))
