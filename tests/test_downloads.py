"""Test cached model reuse and replacement after corruption."""

import hashlib

from komaquill.downloads import fetch_model
from komaquill.workflow import preset


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
