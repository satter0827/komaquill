"""Provide ComfyUI test boundaries without network or GPU dependencies."""

import ipaddress
import os
import shutil
import socket
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest

from komaquill.domain.models import Session
from komaquill.infrastructure.comfyui import ComfyClient
from komaquill.infrastructure.configuration import Project
from tests.support.comfy import ComfyStub
from tests.support.presets import asset


@pytest.fixture(autouse=True)
def offline_network(monkeypatch):
    """Keep accidental real network access outside the daily test suite."""
    connect = socket.socket.connect

    def local_only(connection, address):
        if isinstance(address, tuple):
            try:
                allowed = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                allowed = address[0] == "localhost"
            if not allowed:
                raise AssertionError("External network access is disabled in offline tests.")
        return connect(connection, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture
def project(tmp_path):
    (tmp_path / "komaquill.toml").write_text(
        "poll_interval = 0.001\ngeneration_timeout = 1.0\nstory_max_in_flight = 1\n"
        + "".join(f"[presets.{name}]\nbatch_size = 1\n" for name in ("klein", "klein-edit"))
    )
    return Project(tmp_path)


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


def pytest_configure(config):
    if config.option.basetemp is None:
        (config.rootpath / ".artifacts/tmp").mkdir(parents=True, exist_ok=True)
        config.option.basetemp = config.rootpath / ".artifacts/tmp" / f"pytest-{os.getpid()}"
    (config.rootpath / ".artifacts/records/reports/quality").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def client(stub):
    connection = ComfyClient(stub.session.endpoint, transport=httpx.MockTransport(stub.respond))
    yield connection
    connection.close()


def pytest_sessionfinish(session, exitstatus):
    """Remove only the runner-owned temporary workspace, skipping locked files."""
    folder = session.config.option.basetemp
    if folder is None:
        return
    from pathlib import Path

    folder = Path(folder).resolve()
    allowed = (session.config.rootpath / ".artifacts/tmp").resolve()
    if folder.is_relative_to(allowed) and folder != allowed:
        with suppress(OSError):
            shutil.rmtree(folder)
