"""Verify transfer streaming and persisted deadlines without a GPU or an LLM."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from komaquill.application.generation import submit_generation
from komaquill.domain.errors import KomaquillError
from komaquill.domain.monitor import MonitorState
from komaquill.infrastructure import supervisor
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
        submit_generation(project, client, expired(project, session), "a scene", 1)
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
    extension = extension_api.Extension(project.artifacts, project.models, 1, 60)
    extension.accepting = True
    assert not extension.accepting


def test_invalid_local_model_never_allocates_a_gpu(project, monkeypatch):
    def fail(*args):
        raise KomaquillError(code="invalid_output", stage="preparation", retryable=False)

    monkeypatch.setattr(startup, "check_port", lambda _: None)
    monkeypatch.setattr(startup, "preflight", fail)
    monkeypatch.setattr(
        Colab, "acquire", lambda *args: pytest.fail("Model preparation must finish.")
    )
    with pytest.raises(KomaquillError):
        startup.start_colab(project, lambda _: None)
    assert Colab(project).receipt() is None


@pytest.mark.parametrize("case", ["saved", "timeout", "failed", "cancelled"])
def test_model_transfer_streams_a_path_and_bounds_subprocesses(project, monkeypatch, case):
    import hashlib
    import time
    from contextlib import contextmanager

    import httpx

    from komaquill.infrastructure import asset_download
    from komaquill.infrastructure.configuration import GpuProject
    from komaquill.infrastructure.downloads import fetch_model
    from tests.support.presets import asset

    control = Colab(project)
    receipt = Receipt(
        name="owned", runtime_id=uuid4(), settings=project.settings(), started_at=datetime.now(UTC)
    )
    atomic_json(control.receipt_file, receipt.model_dump(mode="json"))
    gpu = GpuProject(project.root, project.settings())
    content = b"verified GPU model"
    model = asset().model_copy(
        update={"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
    )
    calls = []

    @contextmanager
    def stream(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if case == "failed":
            raise httpx.ReadTimeout("private timeout")
        yield httpx.Response(200, content=content, request=httpx.Request(method, url))

    monkeypatch.setattr(asset_download.httpx, "stream", stream)
    if case == "timeout":
        monkeypatch.setenv("KOMAQUILL_SUBMIT_UNTIL", str(time.time() - 1))
    if case == "cancelled":
        with pytest.raises(KomaquillError) as error:
            asset_download.download_asset(gpu.models, model, 1, lambda: True)
        assert error.value.code == "stopping"
    elif case == "saved":
        assert fetch_model(gpu, model).read_bytes() == content
    else:
        with pytest.raises(KomaquillError) as error:
            fetch_model(gpu, model)
        assert error.value.code == ("preparation_timeout" if case == "timeout" else "http_failed")
    assert not project.models.exists()
    assert control.receipt() == receipt


def test_session_clock_requires_a_time_zone(session):
    with pytest.raises(ValidationError):
        type(session).model_validate({**session.model_dump(), "started_at": datetime.now()})


@pytest.mark.parametrize("overdue", [False, True])
def test_startup_wait_uses_the_owned_gpu_clock(project, monkeypatch, overdue):
    now = datetime.now(UTC)
    row = MonitorState(
        operation_id=uuid4(),
        backend="colab",
        settings=project.settings(),
        created_at=now,
        observed_at=now,
        phase="runtime_owned",
    )
    ready = row.model_copy(update={"phase": "ready"})
    rows = iter([row, ready])
    receipt = SimpleNamespace(
        started_at=now
        - timedelta(seconds=project.settings().submission_timeout + 1 if overdue else 10)
    )
    ticks = iter([0, 10000])
    cancelled = []
    monkeypatch.setattr(supervisor, "read_monitor", lambda _: next(rows))
    monkeypatch.setattr(supervisor, "Colab", lambda _: SimpleNamespace(receipt=lambda: receipt))
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(supervisor.time, "sleep", lambda _: None)
    monkeypatch.setattr(supervisor, "request_cancel", lambda *args: cancelled.append(args))
    if overdue:
        with pytest.raises(KomaquillError):
            supervisor.wait_ready(project, row.operation_id, lambda _: None)
        assert cancelled
    else:
        assert supervisor.wait_ready(project, row.operation_id, lambda _: None) == ready
        assert not cancelled
