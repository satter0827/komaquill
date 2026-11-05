"""Exclude database replacement for every official saver connection."""

import pytest
from komaquill.adapters.persistence.access import acquire
from komaquill.adapters.persistence.connection import database
from komaquill.api import Client, KomaquillError


def test_cached_graph_connection_prevents_database_replacement(tmp_path):
    api = Client.initialize(tmp_path)
    path = tmp_path / ".artifacts/data/komaquill.sqlite3"
    checkpoints = database(path).checkpoints
    checkpoints.saver.get_tuple({"configurable": {"thread_id": "fixture"}})
    try:
        with pytest.raises(KomaquillError, match="operation_busy"):
            acquire(path, exclusive=True, timeout=0).release()
    finally:
        checkpoints.close()
        api.close()
    acquire(path, exclusive=True, timeout=0).release()


@pytest.mark.parametrize("method", ["save", "load", "conversation_result"])
def test_short_lived_savers_obey_exclusive_replacement(tmp_path, monkeypatch, method):
    from komaquill.adapters.persistence import checkpoints as module

    api = Client.initialize(tmp_path)
    path = tmp_path / ".artifacts/data/komaquill.sqlite3"
    store = database(path).checkpoints
    monkeypatch.setattr(module, "acquire", lambda path: acquire(path, timeout=0), raising=False)
    lease = acquire(path, exclusive=True, timeout=0)
    try:
        with pytest.raises(KomaquillError, match="operation_busy"):
            getattr(store, method)("fixture", *([{}] if method == "save" else []))
    finally:
        lease.release()
        api.close()
