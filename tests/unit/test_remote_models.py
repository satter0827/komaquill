"""Verify owned SSH credential handoff without downloading model bytes to the PC."""

import io
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from komaquill.adapters import colab as module
from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.colab import Colab, Receipt
from komaquill.domain.errors import KomaquillError


@pytest.mark.parametrize("owned", [True, False])
def test_credentials_use_stdin_after_owned_identity_verification(project, monkeypatch, owned):
    control = Colab(project)
    receipt = Receipt(name="owned", runtime_id=uuid4(), code_sha256="a" * 64)
    if owned:
        atomic_json(control.receipt_file, receipt.model_dump(mode="json"))
    monkeypatch.setattr(control, "entry", lambda _: {})
    monkeypatch.setattr(control, "ssh_identity", lambda: project.state / "key")
    monkeypatch.setattr(control, "ssh_options", lambda *args: [])
    monkeypatch.setattr(module.shutil, "which", lambda _: "ssh")
    monkeypatch.setattr(module, "credentials", lambda: {"huggingface.co": "private-test-token"})
    calls = []

    class Input(io.BytesIO):
        def close(self):
            self.saved = self.getvalue()

    process = SimpleNamespace(stdin=Input(), wait=lambda **kwargs: 0, returncode=0)

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    if not owned:
        with pytest.raises(KomaquillError):
            control.remote_models(receipt, None)
        assert not calls
    else:
        assert control.remote_models(receipt, None) is process
        assert json.loads(process.stdin.saved)["huggingface.co"] == "private-test-token"
        assert "private-test-token" not in repr(calls)
        assert receipt.code_sha256 in calls[0][0][-1]
        assert not list(project.root.rglob("*.safetensors"))


def test_prepare_transfers_only_code_then_launches_gpu_acquisition(project, monkeypatch):
    control = Colab(project)
    receipt = Receipt(name="owned", runtime_id=uuid4(), code_sha256="a" * 64)
    atomic_json(control.receipt_file, receipt.model_dump(mode="json"))
    calls = []
    monkeypatch.setattr(control, "entry", lambda _: {})
    monkeypatch.setattr(control, "call", lambda *args, **kwargs: calls.append(args))
    process = SimpleNamespace()
    monkeypatch.setattr(control, "remote_models", lambda *args: process)
    assert control.prepare(receipt) is process
    assert [call[0] for call in calls] == ["upload", "exec"]
    assert calls[0][-1].endswith("runtime.tar")
    script = (project.runtime / "deployment/prepare.py").read_text(encoding="utf-8")
    compile(script, "prepare.py", "exec")
    assert ".safetensors" not in script
    assert not list(project.root.rglob("*.safetensors"))
