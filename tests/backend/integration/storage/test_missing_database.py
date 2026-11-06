"""Never interpret missing durable state as a fresh empty workspace."""

import pytest
from komaquill.adapters.persistence.connection import database
from komaquill.api import Client, KomaquillError


@pytest.mark.parametrize("reopen", [False, True])
def test_missing_workspace_database_is_not_recreated(tmp_path, reopen):
    api = Client.initialize(tmp_path)
    api.conversations.list()
    path = tmp_path / ".artifacts/data/komaquill.sqlite3"
    api.close()
    path.unlink()
    if reopen:
        database.cache_clear()
        api = Client(tmp_path)
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.conversations.list()
    assert not path.exists()


def test_reinitialization_cannot_replace_a_missing_database(tmp_path):
    api = Client.initialize(tmp_path)
    api.conversations.list()
    api.close()
    path = tmp_path / ".artifacts/data/komaquill.sqlite3"
    path.unlink()
    with pytest.raises(KomaquillError, match="storage_failed"):
        Client.initialize(tmp_path)
    assert not path.exists()


@pytest.mark.parametrize("version_table", [False, True])
def test_existing_unversioned_database_is_not_initialized(tmp_path, version_table):
    import sqlite3
    from contextlib import closing

    from komaquill.adapters.persistence.connection import Database

    path = tmp_path / "unknown.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE retained (value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('original')")
        if version_table:
            connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
        connection.commit()
    with pytest.raises(KomaquillError, match="storage_failed"):
        Database(path, create=True)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT value FROM retained").fetchall() == [("original",)]
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='conversations'"
        ).fetchall()
