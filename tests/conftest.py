"""Isolate deterministic tests and retain failed workspaces."""

import ipaddress
import os
import socket

import pytest


def loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def offline_network(monkeypatch):
    """Allow connections only to loopback sockets bound by this test process."""
    allowed = set()
    bind = socket.socket.bind
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    resolve = socket.getaddrinfo

    def local_bind(connection, address):
        if isinstance(address, tuple) and not loopback(address[0]):
            raise AssertionError("Tests must bind an explicit loopback address.")
        result = bind(connection, address)
        if isinstance(address, tuple):
            allowed.add(connection.getsockname()[1])
        return result

    def check(address):
        if isinstance(address, tuple) and (not loopback(address[0]) or address[1] not in allowed):
            raise AssertionError("Connect only to a loopback service created by this test.")

    def local_connect(connection, address):
        check(address)
        return connect(connection, address)

    def local_connect_ex(connection, address):
        check(address)
        return connect_ex(connection, address)

    def local_resolve(host, *args, **kwargs):
        if host is not None and not loopback(host):
            raise AssertionError("External name resolution is disabled in deterministic tests.")
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "bind", local_bind)
    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", local_resolve)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    for name in tuple(os.environ):
        if name.startswith(("KOMAQUILL_ALLOW_", "LANGCHAIN_", "LANGSMITH_", "OPENAI_")):
            monkeypatch.delenv(name)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if config.option.basetemp is None:
        temporary = config.rootpath / ".artifacts/tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        os.environ["PYTEST_DEBUG_TEMPROOT"] = str(temporary)
