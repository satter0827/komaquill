"""Verify transfer streaming and persisted deadlines without a GPU or an LLM."""

import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from komaquill.application.generation import submit_generation
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure import colab as transport
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.colab import Colab, Receipt
from komaquill.infrastructure.sessions import monitor, startup
from komaquill.interfaces.comfyui import api as extension_api


def expired(project, session):
    return session.model_copy(
        update={
            "settings": project.settings(),
            "started_at": datetime.now(UTC)
            - timedelta(seconds=project.settings().session_timeout + 1),
        }
    )


def test_expired_session_refuses_submission_before_creating_a_request(
    project,
    session,
    client,
    stub,
):
    with pytest.raises(KomaquillError):
        submit_generation(project, client, expired(project, session), "a scene", None, 1)
    assert stub.submissions == 0 and not project.repository.load()


def test_monitor_stops_without_waiting_for_review_or_a_healthy_connection(
    project,
    session,
    client,
    monkeypatch,
):
    active = expired(project, session)
    stops = []
    monkeypatch.setattr(monitor, "stop_request", lambda *args: None)
    monkeypatch.setattr(monitor, "request_stop", lambda *args: stops.append(args[-1]))
    monkeypatch.setattr(client, "json", lambda *args: pytest.fail("Expired monitoring must stop."))
    assert monitor.watch(project, client, active, lambda _: None) is False
    assert stops == [False]


def test_gpu_extension_deadline_cannot_be_reopened_by_a_local_monitor(project, monkeypatch):
    monkeypatch.setenv("KOMAQUILL_SUBMIT_UNTIL", "100")
    monkeypatch.setattr(extension_api.time, "time", lambda: 101)
    extension = extension_api.Extension(project.artifacts, project.models, 1)
    extension.accepting = True
    assert not extension.accepting


def test_invalid_local_model_never_allocates_a_gpu(project, monkeypatch):
    def fail(*args):
        raise KomaquillError(code="invalid_output", stage="preparation", retryable=False)

    monkeypatch.setattr(startup, "check_port", lambda _: None)
    monkeypatch.setattr(startup, "fetch_model", fail)
    monkeypatch.setattr(
        Colab, "acquire", lambda *args: pytest.fail("Model preparation must finish.")
    )
    with pytest.raises(KomaquillError):
        startup.start_colab(project, lambda _: None)
    assert Colab(project).receipt() is None


@pytest.mark.parametrize("case", ["saved", "timeout", "failed"])
def test_model_transfer_streams_a_path_and_bounds_subprocesses(project, monkeypatch, case):
    control = Colab(project)
    receipt = Receipt(
        name="owned",
        runtime_id=uuid4(),
        settings=project.settings(),
        started_at=datetime.now(UTC) - timedelta(seconds=project.settings().session_timeout - 5),
    )
    atomic_json(control.receipt_file, receipt.model_dump(mode="json"))
    source = project.root / "model.safetensors"
    source.write_bytes(b"test-model")
    calls = []

    def communicate(*, timeout):
        assert 1 <= timeout <= 5
        if case == "timeout":
            raise subprocess.TimeoutExpired("scp", timeout)

    process = SimpleNamespace(communicate=communicate, returncode=1 if case == "failed" else 0)
    monkeypatch.setattr(control, "ssh_identity", lambda: project.root / "key")
    monkeypatch.setattr(control, "ssh_options", lambda *args: ["-o", "BatchMode=yes"])
    monkeypatch.setattr(transport.shutil, "which", lambda _: "scp")
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda args, **kwargs: calls.append(args) or process
    )
    monkeypatch.setattr(transport, "terminate", lambda *args: calls.append("terminated"))
    if case == "saved":
        control.transfer_model(receipt, source, "/content/model.part")
    else:
        with pytest.raises(subprocess.TimeoutExpired if case == "timeout" else KomaquillError):
            control.transfer_model(receipt, source, "/content/model.part")
    assert str(source) in calls[0] and b"test-model" not in calls[0]
    assert control.receipt() == receipt
    assert ("terminated" in calls) == (case == "timeout")


def test_session_clock_requires_a_time_zone(session):
    with pytest.raises(ValidationError):
        type(session).model_validate({**session.model_dump(), "started_at": datetime.now()})
