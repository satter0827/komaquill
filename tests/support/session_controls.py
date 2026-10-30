"""Patch a shared adapter dependency at each session composition boundary."""

from komaquill.runtime import connection, monitor, shutdown, startup


def patch_session(monkeypatch, name, value):
    """Apply the same fake adapter to lifecycle paths exercised by one scenario."""
    found = False
    for module in (connection, monitor, shutdown, startup):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            found = True
    assert found, name
