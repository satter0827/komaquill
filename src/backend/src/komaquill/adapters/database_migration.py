"""Back up a stopped workspace before explicitly upgrading its database."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psutil

from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.database import Database, database
from komaquill.adapters.monitor_state import alive, read_monitor
from komaquill.adapters.storage_errors import stored
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.domain.errors import KomaquillError


def inventory(connection: sqlite3.Connection) -> dict[str, str]:
    result = {}
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if name == "alembic_version":
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        rows = sorted(repr(row) for row in connection.execute(f"SELECT * FROM {quoted}"))
        result[name] = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    return result


def library_preserved(before: sqlite3.Connection, after: sqlite3.Connection) -> bool:
    tables = {row[0] for row in before.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "preferences" not in tables:
        return True
    if (
        before.execute("SELECT version_num FROM alembic_version").fetchall()
        == after.execute("SELECT version_num FROM alembic_version").fetchall()
    ):
        return all(
            before.execute(f"SELECT * FROM {name} ORDER BY id").fetchall()
            == after.execute(f"SELECT * FROM {name} ORDER BY id").fetchall()
            for name in ("preferences", "presets")
        )
    previous = before.execute("SELECT payload FROM preferences WHERE id=1").fetchone()
    if previous is None:
        return True
    configured = json.loads(previous[0])
    chosen = configured.pop("player_id", None)
    configured.pop("image_frequency", None)
    if configured.get("llm_profile") == "fake":
        configured["llm_profile"] = "lmstudio-4b"
    current = after.execute("SELECT payload FROM preferences WHERE id=1").fetchone()
    if current is None or json.loads(current[0]) != configured:
        return False
    original = {
        row[0]: row[1:] for row in before.execute("SELECT id,kind,payload,deleted FROM presets")
    }
    updated = {
        row[0]: row[1:] for row in after.execute("SELECT id,kind,payload,deleted FROM presets")
    }
    active = [identity for identity, row in original.items() if row[0] == "player" and not row[2]]
    if chosen not in active and len(active) == 1:
        chosen = active[0]
    expected = {
        identity: (row[0], row[1], 1 if row[0] == "player" and identity != chosen else row[2])
        for identity, row in original.items()
    }
    return updated == expected


def database_in_use(project: RuntimeWorkspace) -> bool:
    if alive(read_monitor(project)):
        return True
    source = (project.data / "komaquill.sqlite3").resolve()
    for process in psutil.process_iter(["pid", "name"]):
        if process.pid == os.getpid() or not (process.info.get("name") or "").lower().startswith(
            ("python", "pypy")
        ):
            continue
        try:
            if any(Path(item.path).resolve() == source for item in process.open_files()):
                return True
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            return True
    return False


@stored
def migrate(workspace: Path) -> Path:
    project = RuntimeWorkspace(workspace)
    if database_in_use(project):
        raise KomaquillError(code="operation_busy", stage="storage", retryable=False)
    source = project.data / "komaquill.sqlite3"
    if not source.is_file():
        raise KomaquillError(code="request_not_found", stage="storage", retryable=False)
    destination = project.records / "migrations" / str(uuid4())
    destination.mkdir(parents=True)
    backup = destination / "before.sqlite3"
    with closing(sqlite3.connect(source)) as connection, closing(sqlite3.connect(backup)) as copied:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "operations" in tables:
            for (payload,) in connection.execute("SELECT payload FROM operations"):
                if json.loads(payload).get("state") in {"accepted", "running"}:
                    raise KomaquillError(code="operation_busy", stage="storage", retryable=False)
        before = inventory(connection)
        connection.backup(copied)
        if inventory(copied) != before:
            raise KomaquillError(code="storage_failed", stage="storage", retryable=False)
    record = {
        "created_at": datetime.now(UTC).isoformat(),
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "before": before,
        "state": "backed_up",
    }
    report = destination / "migration.json"
    atomic_json(report, record)
    upgraded = Database(source, migrate=True)
    upgraded.engine.dispose()
    with closing(sqlite3.connect(source)) as connection:
        after = inventory(connection)
        with closing(sqlite3.connect(backup)) as copied:
            if not library_preserved(copied, connection):
                raise KomaquillError(code="storage_failed", stage="storage", retryable=False)
    if any(
        after.get(key) != value
        for key, value in before.items()
        if key not in {"presets", "preferences"}
    ):
        raise KomaquillError(code="storage_failed", stage="storage", retryable=False)
    atomic_json(report, {**record, "after": after, "state": "complete"})
    database.cache_clear()
    return report
