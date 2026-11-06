"""Use the official SQLite saver for execution state and validated results."""

import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from functools import cached_property
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from komaquill.adapters.persistence.access import acquire
from komaquill.domain.errors import KomaquillError

RESULT_VERSION = 1
CONVERSATION_WORKFLOW_VERSION = 4


class Checkpoints:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lifetime = ExitStack()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        lease = acquire(self.path)
        try:
            with closing(
                sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=rw", uri=True, check_same_thread=False
                )
            ) as connection:
                yield connection
        finally:
            lease.release()

    @cached_property
    def connection(self) -> sqlite3.Connection:
        return self._lifetime.enter_context(self._connection())

    @cached_property
    def saver(self) -> SqliteSaver:
        saver = SqliteSaver(self.connection)
        saver.setup()
        return saver

    def save(self, key: str, value: dict[str, Any]) -> None:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"result": value, "format_version": RESULT_VERSION}
        checkpoint["channel_versions"] = {"result": 1}
        with self._connection() as connection:
            saver = SqliteSaver(connection)
            saver.put(
                {"configurable": {"thread_id": key, "checkpoint_ns": ""}},
                checkpoint,
                {"source": "update", "step": 0, "parents": {}},
                {"result": 1},
            )

    def load(self, key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            saver = SqliteSaver(connection)
            saved = saver.get_tuple({"configurable": {"thread_id": key}})
        if saved is None:
            return None
        values = saved.checkpoint["channel_values"]
        if values.get("format_version") != RESULT_VERSION or not isinstance(
            values.get("result"), dict
        ):
            raise KomaquillError(code="storage_failed", retryable=False)
        return dict(values["result"])

    def conversation_result(self, key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            saver = SqliteSaver(connection)
            saved = saver.get_tuple({"configurable": {"thread_id": key}})
        if saved is None:
            return None
        values = saved.checkpoint["channel_values"]
        result = values.get("result")
        if values.get("version") != CONVERSATION_WORKFLOW_VERSION:
            raise KomaquillError(code="operation_conflict", retryable=False)
        if result is not None and not isinstance(result, dict):
            raise KomaquillError(code="storage_failed", retryable=False)
        return dict(result) if result is not None else None

    def close(self) -> None:
        if "connection" in self.__dict__:
            self._lifetime.close()
            self.__dict__.pop("connection", None)
            self.__dict__.pop("saver", None)
