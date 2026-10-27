"""Verify frozen settings at the detached GPU preparation entry point."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from filelock import Timeout

from komaquill.gpu import models


@pytest.mark.parametrize("case", ["no_session_deadline", "session_deadline", "already_running"])
def test_gpu_job_uses_frozen_deadlines_and_never_duplicates_a_worker(project, monkeypatch, case):
    identity = uuid4()
    settings = project.settings()
    (project.root / "manifest.json").write_text(
        json.dumps({"settings": settings.model_dump(mode="json")})
    )
    extension = SimpleNamespace(
        path=lambda _: project.root / "job.json", submit_until=None, prepare=AsyncMock()
    )
    calls = []

    def create(*args):
        calls.append(args)
        return extension

    monkeypatch.setattr(models, "Extension", create)
    monkeypatch.setattr(models, "read_credentials", lambda: {"huggingface.co": "offline-fixture"})
    monkeypatch.delenv("KOMAQUILL_SESSION_STARTED_AT", raising=False)
    started = datetime.now(UTC)
    if case == "session_deadline":
        monkeypatch.setenv("KOMAQUILL_SESSION_STARTED_AT", started.isoformat())
    if case == "already_running":

        def locked(*args, **kwargs):
            raise Timeout("offline-worker.lock")

        monkeypatch.setattr(models, "FileLock", locked)
    models.main(project.root, identity)
    assert calls == [
        (
            project.root / ".artifacts",
            project.root / ".artifacts/models",
            settings.request_timeout,
            settings.preparation_timeout,
        )
    ]
    if case == "already_running":
        extension.prepare.assert_not_called()
    else:
        extension.prepare.assert_awaited_once_with(identity, {"huggingface.co": "offline-fixture"})
    assert extension.submit_until == (
        (started + timedelta(seconds=settings.submission_timeout)).timestamp()
        if case == "session_deadline"
        else None
    )
