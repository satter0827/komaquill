"""Protect database replacement and resume individual archive relocations safely."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.paths import WorkspacePaths
from komaquill.adapters.persistence import archive_layout
from komaquill.api import Client, KomaquillError


def test_legacy_archived_operations_are_preserved_with_their_original_owner(tmp_path):
    from alembic import command
    from alembic.config import Config
    from komaquill.adapters.persistence import connection as storage
    from sqlalchemy import create_engine

    from tests.backend.support.storage import sqlite_connection

    Client.initialize(tmp_path).close()
    path = tmp_path / ".artifacts/data/komaquill.sqlite3"
    path.unlink()
    # Initialize an actual preceding schema, rather than weakening the new constraints.
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).with_name("migrations")))
    engine = create_engine("sqlite:///" + path.as_posix())
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0011")
    engine.dispose()
    identity, operation = str(uuid4()), str(uuid4())
    with sqlite_connection(path) as connection:
        connection.execute(
            "INSERT INTO archived_conversations VALUES(?,?,?)", (identity, "{}", "legacy_state")
        )
        connection.execute(
            "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)",
            (
                operation,
                identity,
                "{}",
                "original",
                '{"state":"complete"}',
                None,
                1,
                "original",
                None,
            ),
        )
        original = connection.execute("SELECT * FROM operations").fetchall()
    api = Client(tmp_path)
    api.migrate()
    with sqlite_connection(path) as connection:
        assert connection.execute("SELECT * FROM archived_operations").fetchall() == original
        assert not connection.execute("SELECT * FROM operations").fetchall()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    api.migrate()


def test_open_reader_prevents_replacement_but_released_reader_allows_migration(tmp_path):
    api = Client.initialize(tmp_path)
    storage = api.conversations.repository.database
    with storage.engine.connect() as reader:
        reader.exec_driver_sql("SELECT 1")
        with pytest.raises(KomaquillError, match="operation_busy"):
            api.migrate()
    result = api.migrate()
    assert json.loads(result.read_bytes())["state"] == "complete"


def test_archive_relocation_resumes_after_a_partial_move_without_replacing_bytes(
    tmp_path, monkeypatch
):
    project = WorkspacePaths(tmp_path)
    report = project.records / "migrations/test/migration.json"
    atomic_json(report, {"state": "database_converted"})
    files = {}
    for key in ("runs/first/source.json", "runs/second/source.json"):
        path = project.data / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.encode())
        files[key] = hashlib.sha256(key.encode()).hexdigest()
    archive_layout.begin(project, report, files)
    original = type(path).rename

    def interrupt(source, target):
        if "second" in source.parts:
            raise OSError("Injected move failure")
        return original(source, target)

    with monkeypatch.context() as scoped:
        scoped.setattr(type(path), "rename", interrupt)
        with pytest.raises(OSError):
            archive_layout.finish(project)
    assert (
        project.data / "archive/runs/first/source.json"
    ).read_bytes() == b"runs/first/source.json"
    assert json.loads(archive_layout.pending(project).read_bytes())["status"] == "pending"
    assert archive_layout.finish(project) == report
    result = json.loads(report.read_bytes())
    assert result["state"] == "complete"
    assert result["files_after"] == {"archive/" + key: digest for key, digest in files.items()}


def test_archive_relocation_refuses_a_conflicting_target_and_escaping_report(tmp_path):
    project = WorkspacePaths(tmp_path)
    report = project.records / "migrations/test/migration.json"
    atomic_json(report, {"state": "database_converted"})
    source = project.data / "runs/first/source.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original")
    target = project.data / "archive/runs/first/source.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"different")
    archive_layout.begin(
        project, report, {"runs/first/source.json": hashlib.sha256(b"original").hexdigest()}
    )
    with pytest.raises(KomaquillError, match="identity_mismatch"):
        archive_layout.finish(project)
    assert source.read_bytes() == b"original" and target.read_bytes() == b"different"
    archive_layout.begin(project, tmp_path.parent / "outside.json", {})
    with pytest.raises(KomaquillError, match="invalid_request"):
        archive_layout.finish(project)
