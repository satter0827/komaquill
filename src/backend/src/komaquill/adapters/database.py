"""Store conversation checkpoints in SQLite transactions shared by CLI and HTTP."""

from __future__ import annotations

import builtins
import hashlib
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import psutil
from filelock import FileLock
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    case,
    create_engine,
    func,
    or_,
    select,
)
from sqlalchemy.engine import URL, Connection

from komaquill.adapters.storage_errors import stored
from komaquill.conversations.models import (
    Conversation,
    ConversationInput,
    Operation,
    Reply,
    SendInput,
    StateCommand,
    Turn,
)
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import Run

SQLITE_LOCK_TIMEOUT_SECONDS = 10
_MIGRATION_LOCK = threading.Lock()
metadata = MetaData()
conversations = Table(
    "conversations",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", Text, nullable=False),
)
operations = Table(
    "operations",
    metadata,
    Column("id", String, primary_key=True),
    Column("conversation_id", String, nullable=False),
    Column("request", Text, nullable=False),
    Column("fingerprint", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("active_slot", Integer, unique=True),
    Column("owner_pid", Integer, nullable=False),
    Column("owner_started", String, nullable=False),
    Column("base_revision", Integer),
    Column("reply", Text),
)
images = Table(
    "images",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", Text, nullable=False),
)
image_submissions = Table(
    "image_submissions",
    metadata,
    Column("id", String, primary_key=True),
    Column("fingerprint", String, nullable=False),
)
evaluations = Table(
    "evaluations",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", Text, nullable=False),
)


@lru_cache(maxsize=16)
@stored
def database(path: Path) -> Database:
    return Database(path)


def missing() -> KomaquillError:
    return KomaquillError(code="request_not_found", stage="storage", retryable=False)


def conflict() -> KomaquillError:
    return KomaquillError(code="operation_conflict", stage="storage", retryable=False)


class Database:
    def __init__(self, path: Path, *, migrate: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            URL.create("sqlite", database=str(path.resolve())),
            connect_args={"timeout": SQLITE_LOCK_TIMEOUT_SECONDS},
        )
        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config()
        config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
        with (
            FileLock(str(path.with_suffix(".migration.lock")), timeout=SQLITE_LOCK_TIMEOUT_SECONDS),
            _MIGRATION_LOCK,
            self.engine.begin() as connection,
        ):
            connection.exec_driver_sql("BEGIN IMMEDIATE")
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

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection


class ConversationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @stored
    def create(self, definition: ConversationInput) -> Conversation:
        result = Conversation(definition=definition, state=definition.initial_state)
        with self.database.engine.begin() as connection:
            connection.execute(
                conversations.insert().values(id=str(result.id), payload=result.model_dump_json())
            )
        return result

    @stored
    def list(self) -> list[Conversation]:
        with self.database.engine.connect() as connection:
            values = connection.execute(select(conversations)).mappings().all()
        return sorted(
            (self._conversation(str(value["id"]), str(value["payload"])) for value in values),
            key=lambda value: value.created_at,
            reverse=True,
        )

    def _get(self, connection: Connection, identity: UUID) -> Conversation:
        row = connection.execute(
            select(conversations.c.payload).where(conversations.c.id == str(identity))
        ).scalar_one_or_none()
        if row is None:
            raise missing()
        return self._conversation(str(identity), row)

    def _conversation(self, identity: str, payload: str) -> Conversation:
        value = Conversation.model_validate_json(payload)
        if str(value.id) != identity or len({turn.operation_id for turn in value.turns}) != len(
            value.turns
        ):
            raise ValueError("Conversation identity mismatch.")
        return value

    @stored
    def get(self, identity: UUID) -> Conversation:
        with self.database.engine.connect() as connection:
            return self._get(connection, identity)

    @stored
    def accept(self, identity: UUID, request: SendInput) -> tuple[Operation, bool]:
        fingerprint = hashlib.sha256(
            (str(identity) + request.model_dump_json()).encode()
        ).hexdigest()
        with self.database.transaction() as connection:
            previous = (
                connection.execute(
                    select(operations).where(operations.c.id == str(request.operation_id))
                )
                .mappings()
                .one_or_none()
            )
            if previous is not None:
                if (
                    previous["conversation_id"] != str(identity)
                    or SendInput.model_validate_json(previous["request"]) != request
                ):
                    raise conflict()
                return self._operation(dict(previous)), False
            conversation = self._get(connection, identity)
            if connection.execute(
                select(operations.c.id).where(operations.c.active_slot == 1)
            ).first():
                raise KomaquillError(code="operation_busy", stage="operation", retryable=True)
            result = Operation(id=request.operation_id, conversation_id=identity, state="accepted")
            connection.execute(
                operations.insert().values(
                    id=str(result.id),
                    conversation_id=str(identity),
                    request=request.model_dump_json(),
                    fingerprint=fingerprint,
                    payload=result.model_dump_json(),
                    active_slot=1,
                    owner_pid=os.getpid(),
                    owner_started=str(psutil.Process().create_time()),
                    base_revision=conversation.revision,
                )
            )
            connection.commit()
            return result, True

    def _row(self, connection: Connection, identity: UUID) -> dict[str, object]:
        row = (
            connection.execute(select(operations).where(operations.c.id == str(identity)))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise missing()
        result = dict(row)
        self._operation(result)
        return result

    def _operation(self, row: dict[str, object]) -> Operation:
        value = Operation.model_validate_json(str(row["payload"]))
        request = SendInput.model_validate_json(str(row["request"]))
        fingerprint = hashlib.sha256(
            (str(value.conversation_id) + str(row["request"])).encode()
        ).hexdigest()
        if type(row["owner_pid"]) is not int or int(str(row["owner_pid"])) <= 0:
            raise ValueError("Invalid operation owner.")
        if (
            str(value.id) != row["id"]
            or str(value.conversation_id) != row["conversation_id"]
            or request.operation_id != value.id
            or fingerprint != row["fingerprint"]
            or (row["active_slot"] == 1) != (value.state in {"accepted", "running"})
        ):
            raise ValueError("Operation identity mismatch.")
        return value

    @stored
    def request(self, identity: UUID) -> tuple[Conversation, SendInput]:
        with self.database.engine.connect() as connection:
            row = self._row(connection, identity)
            return self._get(
                connection, UUID(str(row["conversation_id"]))
            ), SendInput.model_validate_json(str(row["request"]))

    @stored
    def operation(self, identity: UUID) -> Operation:
        self.reconcile()
        with self.database.engine.connect() as connection:
            return Operation.model_validate_json(str(self._row(connection, identity)["payload"]))

    @stored
    def operations(self, identity: UUID) -> builtins.list[Operation]:
        with self.database.engine.connect() as connection:
            self._get(connection, identity)
            rows = builtins.list(
                connection.execute(
                    select(operations).where(operations.c.conversation_id == str(identity))
                )
                .mappings()
                .all()
            )
        return sorted(
            (self._operation(dict(row)) for row in rows),
            key=lambda row: row.created_at,
            reverse=True,
        )

    def _save(self, connection: Connection, value: Operation) -> None:
        connection.execute(
            operations.update()
            .where(operations.c.id == str(value.id))
            .values(
                payload=value.model_dump_json(),
                active_slot=1 if value.state in {"accepted", "running"} else None,
            )
        )

    @stored
    def running(self, identity: UUID) -> bool:
        with self.database.transaction() as connection:
            row = self._row(connection, identity)
            result = self._operation(row)
            if (
                result.state != "accepted"
                or row["owner_pid"] != os.getpid()
                or row["owner_started"] != str(psutil.Process().create_time())
            ):
                return False
            self._save(connection, result.model_copy(update={"state": "running"}))
            connection.commit()
            return True

    @stored
    def checkpoint(self, identity: UUID, reply: Reply) -> None:
        with self.database.transaction() as connection:
            row = self._row(connection, identity)
            if self._operation(row).state != "running":
                raise conflict()
            connection.execute(
                operations.update()
                .where(operations.c.id == str(identity))
                .values(reply=reply.model_dump_json())
            )
            connection.commit()

    @stored
    def commit(self, identity: UUID, reply: Reply) -> Operation:
        with self.database.transaction() as connection:
            result = self._commit(connection, identity, reply)
            connection.commit()
            return result

    def _commit(self, connection: Connection, identity: UUID, reply: Reply) -> Operation:
        row = self._row(connection, identity)
        operation = self._operation(row)
        conversation = self._get(connection, operation.conversation_id)
        if operation.state != "running" or row["base_revision"] != conversation.revision:
            raise conflict()
        request = SendInput.model_validate_json(str(row["request"]))
        image = (
            conversation.definition.images if request.images is None else request.images
        ) and bool(reply.scene)
        turn = Turn(
            operation_id=identity,
            user=request.text,
            assistant=reply.text,
            state=reply.state,
            scene=reply.scene,
            image_id=identity if image else None,
        )
        updated = conversation.model_copy(
            update={
                "state": reply.state,
                "turns": [*conversation.turns, turn],
                "revision": conversation.revision + 1,
            }
        )
        connection.execute(
            conversations.update()
            .where(conversations.c.id == str(conversation.id))
            .values(payload=updated.model_dump_json())
        )
        result = operation.model_copy(
            update={"state": "complete", "image_state": "pending" if image else "disabled"}
        )
        self._save(connection, result)
        return result

    @stored
    def fail(self, identity: UUID, code: str) -> Operation:
        with self.database.transaction() as connection:
            result = Operation.model_validate_json(str(self._row(connection, identity)["payload"]))
            if result.state in {"accepted", "running"}:
                result = result.model_copy(update={"state": "failed", "error": code})
                self._save(connection, result)
            connection.commit()
            return result

    @stored
    def image(self, identity: UUID, image_id: UUID | None, error: str | None = None) -> None:
        with self.database.transaction() as connection:
            result = Operation.model_validate_json(str(self._row(connection, identity)["payload"]))
            self._save(
                connection,
                result.model_copy(
                    update={
                        "image_state": "submitted" if image_id else "failed",
                        "image_id": image_id,
                        "image_error": error,
                    }
                ),
            )
            connection.commit()

    @stored
    def command(self, identity: UUID, command: StateCommand) -> Conversation:
        with self.database.transaction() as connection:
            conversation = self._get(connection, identity)
            active = connection.execute(
                select(operations.c.id).where(operations.c.active_slot == 1)
            ).first()
            if active or conversation.revision != command.expected_revision:
                raise conflict()
            state = (
                conversation.definition.initial_state
                if command.command == "reset_state"
                else command.state
            )
            result = conversation.model_copy(
                update={"state": state, "revision": conversation.revision + 1}
            )
            connection.execute(
                conversations.update()
                .where(conversations.c.id == str(identity))
                .values(payload=result.model_dump_json())
            )
            connection.commit()
            return result

    @stored
    def reconcile(self) -> None:
        with self.database.transaction() as connection:
            for row in connection.execute(
                select(operations).where(
                    or_(
                        operations.c.active_slot == 1,
                        case(
                            (
                                func.json_valid(operations.c.payload) == 1,
                                func.json_extract(operations.c.payload, "$.image_state"),
                            ),
                            else_=None,
                        )
                        == "pending",
                    )
                )
            ).mappings():
                try:
                    value = self._operation(dict(row))
                    submitted = self._submitted_image(connection, value)
                except ValueError:
                    if row["active_slot"] == 1:
                        raise
                    continue
                if submitted is not None:
                    self._save(connection, submitted)
                    continue
                try:
                    alive = (
                        str(psutil.Process(row["owner_pid"]).create_time()) == row["owner_started"]
                    )
                except psutil.NoSuchProcess:
                    alive = False
                except psutil.Error:
                    continue
                if not alive:
                    if value.state == "running" and row["reply"] is not None:
                        try:
                            value = self._commit(
                                connection, value.id, Reply.model_validate_json(row["reply"])
                            )
                        except KomaquillError as error:
                            if error.code != "operation_conflict":
                                raise
                            value = value.model_copy(
                                update={"state": "interrupted", "error": "operation_conflict"}
                            )
                        if value.image_state != "pending":
                            self._save(connection, value)
                            continue
                    changes: dict[str, object] = (
                        {"state": "interrupted", "error": "submission_unknown"}
                        if value.state in {"accepted", "running"}
                        else {"image_state": "failed", "image_error": "submission_unknown"}
                    )
                    if (
                        value.state == "complete"
                        and self._submitted_image(connection, value) is not None
                    ):
                        changes = {"image_state": "submitted", "image_id": value.id}
                    self._save(
                        connection,
                        value.model_copy(update=changes),
                    )
            connection.commit()

    def _submitted_image(self, connection: Connection, operation: Operation) -> Operation | None:
        if operation.state != "complete" or operation.image_state != "pending":
            return None
        payload = connection.execute(
            select(images.c.payload).where(images.c.id == str(operation.id))
        ).scalar_one_or_none()
        if payload is None:
            return None
        run = Run.model_validate_json(payload)
        if run.run_id != operation.id:
            raise ValueError("Image identity mismatch.")
        if run.state == "prepared":
            return None
        return operation.model_copy(update={"image_state": "submitted", "image_id": operation.id})
