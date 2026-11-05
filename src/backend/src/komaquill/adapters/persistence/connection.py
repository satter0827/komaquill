"""Own SQLite connections, transactions and execution locks."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cached_property, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from filelock import FileLock, Timeout
from sqlalchemy import (
    create_engine,
    event,
)
from sqlalchemy.engine import URL, Connection
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.pool import ConnectionPoolEntry, NullPool

if TYPE_CHECKING:
    from komaquill.adapters.persistence.checkpoints import Checkpoints
    from komaquill.adapters.persistence.policies import ContentPolicy

from komaquill.adapters.persistence.access import acquire
from komaquill.adapters.storage_errors import stored
from komaquill.domain.errors import KomaquillError

SQLITE_LOCK_TIMEOUT_SECONDS = 10


_MIGRATION_LOCK = threading.Lock()


@lru_cache(maxsize=16)
@stored
def database(path: Path) -> Database:
    return Database(path)


class Database:
    @cached_property
    def content(self) -> ContentPolicy:
        from komaquill.adapters.persistence.policies import ContentPolicy

        return ContentPolicy(self.write)

    @cached_property
    def checkpoints(self) -> Checkpoints:
        from komaquill.adapters.persistence.checkpoints import Checkpoints

        return Checkpoints(self.path)

    def __init__(self, path: Path, *, migrate: bool = False) -> None:
        self.path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            URL.create("sqlite", database=str(path.resolve())),
            connect_args={"timeout": SQLITE_LOCK_TIMEOUT_SECONDS},
            poolclass=NullPool,
        )

        @event.listens_for(self.engine, "do_connect")
        def open_connection(
            _: Dialect, record: ConnectionPoolEntry, args: list[Any], values: dict[str, Any]
        ) -> sqlite3.Connection:
            lease = acquire(self.path)
            try:
                connection = sqlite3.connect(*args, **values)
                assert isinstance(connection, sqlite3.Connection)
            except BaseException:
                lease.release()
                raise
            record.info["access_lease"] = lease
            return connection

        @event.listens_for(self.engine, "close")
        def release_connection(_: object, record: ConnectionPoolEntry) -> None:
            lease = record.info.pop("access_lease", None)
            if lease is not None:
                lease.release()

        @event.listens_for(self.engine, "connect")
        def configure_connection(connection: object, _: object) -> None:
            assert isinstance(connection, sqlite3.Connection)
            connection.isolation_level = None
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA journal_mode=WAL")

        @event.listens_for(self.engine, "begin")
        def begin_transaction(connection: Connection) -> None:
            connection.exec_driver_sql(
                "BEGIN IMMEDIATE" if connection.info.get("immediate") else "BEGIN"
            )

        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config()
        config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
        with (
            FileLock(str(path.with_suffix(".migration.lock")), timeout=SQLITE_LOCK_TIMEOUT_SECONDS),
            _MIGRATION_LOCK,
            self.engine.connect() as connection,
        ):
            connection.info["immediate"] = True
            connection.begin()
            if connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name='alembic_version'"
            ).first():
                current = connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar()
                if (
                    current
                    and current != ScriptDirectory.from_config(config).get_current_head()
                    and not migrate
                ):
                    raise KomaquillError(
                        code="migration_required", stage="storage", retryable=False
                    )
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()

    @contextmanager
    def write(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            connection.info["immediate"] = True
            with connection.begin():
                yield connection

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            connection.info["immediate"] = True
            connection.begin()
            yield connection

    @contextmanager
    def execution_lock(self, identity: UUID) -> Iterator[bool]:
        lock = FileLock(str(self.path.with_name(f".{self.path.name}.{identity}.execution.lock")))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            yield False
            return
        except OSError:
            raise KomaquillError(code="storage_failed", stage="storage", retryable=False) from None
        try:
            yield True
        finally:
            lock.release()
