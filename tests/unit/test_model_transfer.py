"""Bound transfer estimates and reuse only remotely verified asset bytes."""

import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure import colab as module
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.colab import Colab, Receipt
from tests.support.models import stage_models


@pytest.mark.parametrize("case", ["fits", "too_slow", "interrupted"])
def test_probe_stops_before_full_transfer_when_it_cannot_fit(project, monkeypatch, case):
    control = Colab(project)
    receipt = Receipt(name="owned", runtime_id=uuid4(), started_at=datetime.now(UTC))
    monkeypatch.setattr(module, "TRANSFER_PROBE_BYTES", 1024 * 1024)
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    transfers = []

    def transfer(_, source, destination, *, timeout):
        transfers.append(source.stat().st_size)
        assert timeout == 60
        if case == "interrupted":
            raise subprocess.TimeoutExpired("scp", timeout)

    monkeypatch.setattr(control, "transfer_model", transfer)
    if case == "interrupted":
        with pytest.raises(subprocess.TimeoutExpired):
            control.check_transfer_feasibility(receipt, 100 * 1024**2)
    elif case == "too_slow":
        with pytest.raises(KomaquillError) as error:
            control.check_transfer_feasibility(receipt, 100 * 1024**3)
        assert error.value.display_key == "model.transfer_deadline"
    else:
        control.check_transfer_feasibility(receipt, 100 * 1024**2)
    assert transfers == [1024 * 1024]
    assert not list((project.artifacts / "tmp").glob("transfer-probe-*.bin"))
    if case != "interrupted":
        record = json.loads(
            next(
                (project.logs / str(receipt.runtime_id)).glob("transfer-probe-*.json")
            ).read_bytes()
        )
        assert record["feasible"] == (case == "fits")


@pytest.mark.parametrize(
    "output,code,expected",
    [
        (b"", 0, None),
        (b"a" * 64 + b"\n", 0, "a" * 64),
        (b"private response", 0, "error"),
        (b"", 1, "error"),
    ],
)
def test_remote_hash_never_treats_a_transport_error_as_a_cache_miss(
    project, monkeypatch, output, code, expected
):
    control = Colab(project)
    receipt = Receipt(name="owned", runtime_id=uuid4())
    monkeypatch.setattr(control, "ssh_identity", lambda: project.state / "key")
    monkeypatch.setattr(control, "ssh_options", lambda *args: [])
    monkeypatch.setattr(module.shutil, "which", lambda _: "ssh")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=output, returncode=code),
    )
    if expected == "error":
        with pytest.raises(KomaquillError) as error:
            control.remote_digest(receipt, "/content/model.safetensors")
        assert "private" not in str(error.value)
    else:
        assert control.remote_digest(receipt, "/content/model.safetensors") == expected


def test_preparation_reuses_verified_remote_assets_without_sending_them_again(project, monkeypatch):
    from komaquill.infrastructure import deployment

    monkeypatch.setattr(deployment, "source_revision", lambda: "test")
    stage_models(project)
    control = Colab(project)
    receipt = Receipt(
        name="owned", runtime_id=uuid4(), endpoint_id="expected", account_sha256="a" * 64
    )
    atomic_json(control.receipt_file, receipt.model_dump(mode="json"))
    monkeypatch.setattr(control, "entry", lambda _: {})
    monkeypatch.setattr(control, "call", lambda *args, **kwargs: None)
    monkeypatch.setattr(control, "remote_digest", lambda _, path: path.split("/")[-2])
    monkeypatch.setattr(
        control,
        "transfer_model",
        lambda *args, **kwargs: pytest.fail("Verified models must be reused"),
    )
    monkeypatch.setattr(
        control,
        "check_transfer_feasibility",
        lambda *args: pytest.fail("No model bytes need transfer"),
    )
    monkeypatch.setattr(control, "command", lambda *args: ["official", *args])
    process = SimpleNamespace()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    assert control.prepare(receipt) is process
    script = (project.runtime / "deployment/prepare.py").read_text(encoding="utf-8")
    assert "'staging': None" in script
    compile(script, "prepare.py", "exec")
