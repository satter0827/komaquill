"""Provide explicit backend fixtures without changing model counting defaults."""

from uuid import uuid4

import httpx
import pytest
from komaquill.adapters.comfyui import ComfyClient
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.domain.models import Session

from tests.backend.support.comfy import ComfyStub
from tests.backend.support.presets import asset


@pytest.fixture
def scripted_services(monkeypatch, scripted_counting):
    """Pair substituted inference and rendering services with read-only observations."""
    from komaquill.api.availability import AvailabilityService

    monkeypatch.setattr(AvailabilityService, "_llm", lambda *_: None)
    monkeypatch.setattr(AvailabilityService, "_comfy", lambda *_: None)


@pytest.fixture
def stopped_database(monkeypatch):
    """Isolate migration data contracts from unrelated host processes."""
    from komaquill.adapters.persistence import migrate as database_migration

    monkeypatch.setattr(database_migration, "file_users", lambda _: set())


@pytest.fixture
def project(tmp_path, monkeypatch):
    from komaquill.adapters import deployment
    from komaquill.adapters.persistence.connection import Database

    from tests.backend.support.runtime_tools import archive

    monkeypatch.setattr(deployment, "runtime_archive", lambda _: archive())
    Database(tmp_path / ".artifacts/data/komaquill.sqlite3", create=True).engine.dispose()
    (tmp_path / "komaquill.toml").write_text(
        'models = ["klein-4b"]\npoll_interval = 0.001\ngeneration_timeout = 1.0\n'
        "story_max_in_flight = 1\n"
        + "".join(f"[presets.{name}]\nbatch_size = 1\n" for name in ("klein-4b", "klein-4b-edit"))
    )
    return RuntimeWorkspace(tmp_path)


@pytest.fixture
def session():
    return Session(
        endpoint="http://127.0.0.1:18188",
        runtime_id=uuid4(),
        source_revision="a" * 40,
        comfy_revision="b" * 40,
        frontend_version="1.55.18",
        asset_sha256={asset().name: asset().sha256},
    )


@pytest.fixture
def stub(session):
    return ComfyStub(session.model_copy(deep=True))


@pytest.fixture
def client(stub):
    connection = ComfyClient(stub.session.endpoint, transport=httpx.MockTransport(stub.respond))
    yield connection
    connection.close()


@pytest.fixture
def scripted_counting(monkeypatch):
    """Declare approximate counting only for explicitly scripted SDK fixtures."""
    monkeypatch.setenv(
        "KOMAQUILL_CONVERSATION__LLM_PROFILES__LMSTUDIO-4B__TOKEN_COUNTING", "approximate"
    )
