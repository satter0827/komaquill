"""Bound detached preparation workers independently from SSH and local polling."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from komaquill.domain.assets import Preparation
from komaquill.domain.errors import FailureInfo, KomaquillError
from komaquill.infrastructure.atomic import atomic_json
from komaquill.interfaces.comfyui import api as module
from tests.unit.test_assets import asset


def job_for(extension, **values):
    job = Preparation(
        workspace_id=uuid4(), workflow={}, phase="confirmed", assets=[asset()], **values
    )
    atomic_json(extension.path(job.request_id), job.model_dump(mode="json"))
    return job


@pytest.mark.parametrize("deadline", ["job", "session"])
def test_expired_job_cannot_start_a_download(project, monkeypatch, deadline):
    extension = module.Extension(project.artifacts, project.models, 1, 1)
    job = job_for(
        extension,
        created_at=datetime.now(UTC) - timedelta(seconds=2 if deadline == "job" else 0),
    )
    if deadline == "session":
        extension.submit_until = 100
        monkeypatch.setattr(module.time, "time", lambda: 101)
    calls = []
    monkeypatch.setattr(module, "download_asset", lambda *args: calls.append(args))
    asyncio.run(extension.prepare(job.request_id, {}))
    final = extension.read(job.request_id)
    assert not calls
    assert final.phase == "failed" and final.failure.code == "preparation_timeout"


@pytest.mark.parametrize("deadline", ["job", "session"])
def test_detached_download_deadline_does_not_wait_for_the_http_thread(
    project, monkeypatch, deadline
):
    extension = module.Extension(project.artifacts, project.models, 1, 60)
    job = job_for(extension)
    limit = 60 if deadline == "job" else 5
    if deadline == "session":
        extension.submit_until = 105
        monkeypatch.setattr(module.time, "time", lambda: 100)
    release, started, stopped = threading.Event(), threading.Event(), threading.Event()
    cancellation, contexts = [], []

    def deadline(delay):
        context = asyncio.Timeout(None)
        contexts.append((delay, context))
        return context

    def download(*args):
        started.set()
        release.wait(timeout=5)
        cancellation.append(args[3]())
        stopped.set()

    monkeypatch.setattr(module, "download_asset", download)
    monkeypatch.setattr(module.asyncio, "timeout", deadline)

    async def scenario():
        task = asyncio.create_task(extension.prepare(job.request_id, {}))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert len(contexts) == 1 and 0 < contexts[0][0] <= limit
            contexts[0][1].reschedule(asyncio.get_running_loop().time())
            await asyncio.wait_for(task, timeout=2)
            final = extension.read(job.request_id)
            assert final.phase == "failed" and final.failure.code == "preparation_timeout"
            assert not stopped.is_set()
        finally:
            release.set()
        assert await asyncio.to_thread(stopped.wait, 2)

    asyncio.run(scenario())
    assert cancellation == [True]


def test_failed_job_interrupts_an_existing_download_without_overwriting_its_reason(
    project, monkeypatch
):
    extension = module.Extension(project.artifacts, project.models, 1, 60)
    job = job_for(extension)
    cancelled = []
    expected = FailureInfo(
        code="connection_failed",
        stage="preparation",
        retryable=False,
        exception_type="KomaquillError",
    )

    def download(*args):
        extension.update(job.request_id, phase="failed", failure=expected)
        cancelled.append(args[3]())
        raise KomaquillError(code="stopping", stage="preparation")

    monkeypatch.setattr(module, "download_asset", download)
    asyncio.run(extension.prepare(job.request_id, {}))
    assert cancelled == [True]
    assert extension.read(job.request_id).failure == expected
