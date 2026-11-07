"""Persist conversation snapshots, history and accepted operations."""

from __future__ import annotations

import builtins
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

import psutil
from sqlalchemy import (
    case,
    func,
    or_,
    select,
)
from sqlalchemy.engine import Connection

if TYPE_CHECKING:
    pass

from komaquill.adapters.persistence.connection import Database
from komaquill.adapters.persistence.conversation_records import (
    read_action,
    read_bookmark,
    read_conversation,
    read_turn,
)
from komaquill.adapters.persistence.errors import conflict, missing
from komaquill.adapters.persistence.schema import (
    command_receipts,
    conversation_bookmarks,
    conversation_events,
    conversation_turns,
    conversations,
    images,
    operations,
)
from komaquill.adapters.storage_errors import stored
from komaquill.domain.conversations.actions import (
    ActionRecord,
    InferredMove,
    ProposalDismissal,
    ProposedAction,
    RecordedActionRequest,
)
from komaquill.domain.conversations.commands import accept_input
from komaquill.domain.conversations.editing import Bookmark, NotesRequest, SceneRequest
from komaquill.domain.conversations.history import advance_history, memory_sources
from komaquill.domain.conversations.meetings import MeetingAnswer, MeetingPlan
from komaquill.domain.conversations.models import (
    TURN_RESULT,
    AcceptedInput,
    Conversation,
    ConversationInput,
    Operation,
    Reply,
    SendInput,
    Turn,
    TurnRequest,
)
from komaquill.domain.conversations.transitions import apply_reply
from komaquill.domain.conversations.visuals import VisualReviewRecord
from komaquill.domain.errors import KomaquillError, OperationStage
from komaquill.domain.models import Run
from komaquill.domain.roleplay import ConversationMemory


class RecordedTurnRequest(TurnRequest):
    """Validate retained request metadata independently of today's send contract."""

    images: bool | None = None
    response_length: Literal["short", "standard", "detailed"] | None = None


DEFAULT_TURN_PAGE_SIZE = 30


MAX_TURN_PAGE_SIZE = 100


class ConversationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def _store(self, connection: Connection, value: Conversation, *, create: bool = False) -> None:
        count = max(value.turn_count, value.turn_offset + len(value.turns))
        payload = value.model_copy(update={"turn_count": count, "turn_offset": 0}).model_dump_json(
            exclude={
                "turns",
                "history_prefix_sha256",
                "history_anchor",
                "actions",
                "proposals",
                "visual_reviews",
            }
        )
        statement = (
            conversations.insert().values(id=str(value.id), payload=payload)
            if create
            else (
                conversations.update()
                .where(conversations.c.id == str(value.id))
                .values(payload=payload)
            )
        )
        connection.execute(statement)
        existing = connection.execute(
            select(func.count())
            .select_from(conversation_turns)
            .where(conversation_turns.c.conversation_id == str(value.id))
        ).scalar_one()
        prefix = (
            connection.execute(
                select(conversation_turns.c.prefix_sha256).where(
                    conversation_turns.c.conversation_id == str(value.id),
                    conversation_turns.c.position == existing - 1,
                )
            ).scalar_one_or_none()
            or ""
        )
        for position, turn in enumerate(
            value.turns[max(0, existing - value.turn_offset) :], max(existing, value.turn_offset)
        ):
            source = turn.model_dump(mode="json")
            if turn.meeting_state is not None:
                source["meeting_state"] = turn.meeting_state.model_dump(mode="json")
            prefix = advance_history(prefix, source)
            connection.execute(
                conversation_turns.insert().values(
                    conversation_id=str(value.id),
                    position=position,
                    turn_id=str(turn.operation_id),
                    payload=json.dumps(source, ensure_ascii=False),
                    prefix_sha256=prefix,
                )
            )

        from sqlalchemy.dialects.sqlite import insert

        for kind in ("actions", "proposals"):
            for event in getattr(value, kind):
                event_id = (
                    event.id if isinstance(event, ProposedAction) else event.request.operation_id
                )
                if kind == "actions":
                    previous = self._event_payload(connection, value.id, kind, event_id)
                    if previous is not None:
                        if read_action(previous) != event:
                            raise ValueError("A recorded action cannot change.")
                        continue
                statement = insert(conversation_events).values(
                    conversation_id=str(value.id),
                    kind=kind,
                    id=str(event_id),
                    payload=event.model_dump_json(),
                    pending=int(isinstance(event, ProposedAction) and event.state == "pending"),
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["conversation_id", "kind", "id"],
                        set_={
                            "payload": statement.excluded.payload,
                            "pending": statement.excluded.pending,
                        },
                        where=conversation_events.c.payload != statement.excluded.payload,
                    )
                )

    @contextmanager
    def execution(self, identity: UUID) -> Iterator[bool]:
        with self.database.execution_lock(identity) as acquired:
            yield acquired and self.running(identity)

    def _command_key(
        self,
        identity: UUID,
        kind: str,
        request: RecordedActionRequest | ProposalDismissal | NotesRequest | SceneRequest,
    ) -> str:
        return hashlib.sha256(
            (str(identity) + kind + request.model_dump_json()).encode()
        ).hexdigest()

    def _known_command(
        self,
        connection: Connection,
        identity: UUID,
        kind: str,
        request: RecordedActionRequest | ProposalDismissal | NotesRequest | SceneRequest,
    ) -> bool:
        operation_id = str(request.operation_id)
        previous = connection.execute(
            select(command_receipts.c.fingerprint).where(command_receipts.c.id == operation_id)
        ).scalar_one_or_none()
        if previous is not None:
            if previous != self._command_key(identity, kind, request):
                raise conflict()
            return True
        if connection.execute(
            select(operations.c.id).where(operations.c.id == operation_id)
        ).first():
            raise conflict()
        return False

    def _register_command(
        self,
        connection: Connection,
        identity: UUID,
        kind: str,
        request: RecordedActionRequest | ProposalDismissal | NotesRequest | SceneRequest,
    ) -> None:
        connection.execute(
            command_receipts.insert().values(
                id=str(request.operation_id), fingerprint=self._command_key(identity, kind, request)
            )
        )

    def _event_payload(
        self, connection: Connection, identity: UUID, kind: str, event_id: UUID
    ) -> str | None:
        return connection.execute(
            select(conversation_events.c.payload).where(
                conversation_events.c.conversation_id == str(identity),
                conversation_events.c.kind == kind,
                conversation_events.c.id == str(event_id),
            )
        ).scalar_one_or_none()

    def _bind_action(
        self, connection: Connection, identity: UUID, request: RecordedTurnRequest
    ) -> RecordedTurnRequest:
        action_id = request.action_id or request.operation_id
        payload = self._event_payload(connection, identity, "actions", action_id)
        if payload is None:
            if request.action is not None or request.action_id is not None:
                raise ValueError("The accepted action record is missing.")
            return request
        recorded = read_action(payload)
        if (
            recorded.request.operation_id != action_id
            or recorded.reaction_id != request.operation_id
            or (request.action is not None and recorded.request.action != request.action)
            or (request.action is not None and recorded.origin != "explicit")
            or (
                request.action is None
                and request.action_id is None
                and recorded.origin != "conversation"
            )
        ):
            raise ValueError("The accepted action identity differs from its request.")
        if recorded.source is not None:
            if recorded.origin != "conversation":
                raise ValueError("Only inferred moves have a source span.")
            recorded.source.read(request.text)
        return request.model_copy(update={"action_id": action_id, "action_origin": recorded.origin})

    @stored
    def apply_inferred_move(self, identity: UUID, move: InferredMove) -> None:
        """Apply a saved interpretation once without changing the accepted message."""
        with self.database.transaction() as connection:
            row = self._row(connection, identity)
            operation = self._operation(row)
            request = RecordedTurnRequest.model_validate_json(str(row["request"]))
            move.source.read(request.text)
            previous = self._event_payload(
                connection, operation.conversation_id, "actions", identity
            )
            if previous is not None:
                recorded = read_action(previous)
                if (
                    recorded.origin != "conversation"
                    or recorded.request.action != move.action
                    or recorded.source != move.source
                ):
                    raise conflict()
                return
            conversation = self._get(connection, operation.conversation_id, with_turns=False)
            if (
                operation.state != "running"
                or row["base_revision"] != conversation.revision
                or request.action is not None
                or request.action_id is not None
            ):
                raise conflict()
            if move.action.destination.strip() == conversation.state.place.strip():
                raise KomaquillError(code="invalid_response", retryable=False)
            updated = accept_input(
                conversation,
                SendInput(
                    operation_id=identity,
                    expected_revision=conversation.revision,
                    text=request.text,
                    action=move.action,
                    locale=request.locale,
                    allow_real_llm=request.allow_real_llm,
                ),
            )
            updated = updated.model_copy(
                update={
                    "actions": [
                        *updated.actions[:-1],
                        updated.actions[-1].model_copy(
                            update={"origin": "conversation", "source": move.source}
                        ),
                    ]
                }
            )
            self._store(connection, updated)
            connection.execute(
                operations.update()
                .where(operations.c.id == str(identity))
                .values(base_revision=updated.revision)
            )
            connection.commit()

    @stored
    def actions(self, identity: UUID) -> builtins.list[ActionRecord]:
        with self.database.engine.connect() as reader:
            self._get(reader, identity, with_turns=False)
            rows = reader.execute(
                select(conversation_events.c.id, conversation_events.c.payload).where(
                    conversation_events.c.conversation_id == str(identity),
                    conversation_events.c.kind == "actions",
                )
            )
            values = []
            for row in rows:
                value = read_action(str(row.payload))
                if str(value.request.operation_id) != row.id:
                    raise ValueError("Action identity mismatch.")
                values.append(value)
            return sorted(values, key=lambda value: value.revision)

    @stored
    def create(self, definition: ConversationInput) -> Conversation:
        result = Conversation(definition=definition, state=definition.initial_state)
        with self.database.write() as connection:
            self._store(connection, result, create=True)
        return result

    @stored
    def list(self) -> list[Conversation]:
        with self.database.engine.connect() as connection:
            values = connection.execute(select(conversations)).mappings().all()
            return sorted(
                (
                    self._conversation(
                        connection, str(value["id"]), str(value["payload"]), with_turns=False
                    )
                    for value in values
                ),
                key=lambda value: value.created_at,
                reverse=True,
            )

    def _get(
        self,
        connection: Connection,
        identity: UUID,
        *,
        with_turns: bool = True,
        from_turn: int = 0,
        with_events: bool | None = None,
    ) -> Conversation:
        row = connection.execute(
            select(conversations.c.payload).where(conversations.c.id == str(identity))
        ).scalar_one_or_none()
        if row is None:
            raise missing()
        return self._conversation(
            connection,
            str(identity),
            row,
            with_turns=with_turns,
            from_turn=from_turn,
            with_events=with_turns if with_events is None else with_events,
        )

    def _conversation(
        self,
        reader: Connection,
        identity: str,
        payload: str,
        *,
        with_turns: bool = True,
        from_turn: int = 0,
        with_events: bool | None = None,
    ) -> Conversation:
        value = read_conversation(payload)
        query = select(conversation_events).where(conversation_events.c.conversation_id == identity)
        if not (with_turns if with_events is None else with_events):
            query = query.where(conversation_events.c.pending == 1)
        events: dict[str, list[ActionRecord | ProposedAction | VisualReviewRecord]] = {
            "actions": [],
            "proposals": [],
            "visual_reviews": [],
        }
        models: dict[str, type[ActionRecord] | type[ProposedAction] | type[VisualReviewRecord]] = {
            "actions": ActionRecord,
            "proposals": ProposedAction,
            "visual_reviews": VisualReviewRecord,
        }
        for row in reader.execute(query).mappings():
            event = (
                read_action(row["payload"])
                if row["kind"] == "actions"
                else models[row["kind"]].model_validate_json(row["payload"])
            )
            event_id = event.id if isinstance(event, ProposedAction) else event.request.operation_id
            if str(event_id) != row["id"]:
                raise ValueError("Conversation event identity mismatch.")
            events[row["kind"]].append(event)
        for records in events.values():
            records.sort(key=lambda event: event.revision)
        value = value.model_copy(update=events)
        if with_turns:
            loaded, prefix, anchor_id = self._read_turns(
                reader, identity, from_turn, value.turn_count
            )
            value = value.model_copy(
                update={
                    "turns": loaded,
                    "turn_offset": from_turn,
                    "history_prefix_sha256": prefix,
                    "history_anchor": f"{anchor_id}:assistant" if anchor_id else "",
                }
            )
        if str(value.id) != identity or len({turn.operation_id for turn in value.turns}) != len(
            value.turns
        ):
            raise ValueError("Conversation identity mismatch.")
        return value

    def _read_turns(
        self, reader: Connection, identity: str, start: int, end: int
    ) -> tuple[builtins.list[Turn], str, str]:
        if not 0 <= start <= end:
            raise ValueError("Invalid history range.")
        prefix, anchor_id = "", ""
        if start:
            anchor = reader.execute(
                select(conversation_turns.c.prefix_sha256, conversation_turns.c.turn_id).where(
                    conversation_turns.c.conversation_id == identity,
                    conversation_turns.c.position == start - 1,
                )
            ).one_or_none()
            if anchor is None:
                raise ValueError("Conversation history is incomplete.")
            prefix, anchor_id = str(anchor[0]), str(anchor[1])
        original_prefix = prefix
        rows = (
            reader.execute(
                select(conversation_turns)
                .where(
                    conversation_turns.c.conversation_id == identity,
                    conversation_turns.c.position >= start,
                    conversation_turns.c.position < end,
                )
                .order_by(conversation_turns.c.position)
            )
            .mappings()
            .all()
        )
        if len(rows) != end - start:
            raise ValueError("Conversation history is incomplete.")
        values = []
        for position, row in enumerate(rows, start):
            source = json.loads(row["payload"])
            turn = read_turn(source)
            prefix = advance_history(prefix, source)
            if (
                row["position"] != position
                or row["turn_id"] != str(turn.operation_id)
                or row["prefix_sha256"] != prefix
            ):
                raise ValueError("Conversation history identity mismatch.")
            values.append(turn)
        if len({turn.operation_id for turn in values}) != len(values):
            raise ValueError("Conversation history identity mismatch.")
        return values, original_prefix, anchor_id

    @stored
    def history_hash(self, identity: UUID, through: int) -> str:
        if through == 0:
            return ""
        with self.database.engine.connect() as reader:
            prefix = reader.execute(
                select(conversation_turns.c.prefix_sha256).where(
                    conversation_turns.c.conversation_id == str(identity),
                    conversation_turns.c.position == through - 1,
                )
            ).scalar_one_or_none()
        if prefix is None:
            raise ValueError("Conversation history is incomplete.")
        return str(prefix)

    @stored
    def get(self, identity: UUID) -> Conversation:
        with self.database.engine.connect() as connection:
            return self._get(connection, identity)

    @stored
    def overview(self, identity: UUID) -> Conversation:
        with self.database.engine.connect() as connection:
            return self._get(connection, identity, with_turns=False)

    @stored
    def turns(
        self, identity: UUID, before: int | None = None, limit: int = DEFAULT_TURN_PAGE_SIZE
    ) -> dict[str, object]:
        if limit < 1 or limit > MAX_TURN_PAGE_SIZE or (before is not None and before < 0):
            raise KomaquillError(code="invalid_request", retryable=False)
        with self.database.engine.connect() as reader:
            conversation = self._get(reader, identity, with_turns=False)
            end = min(
                before if before is not None else conversation.turn_count, conversation.turn_count
            )
            start = max(0, end - limit)
            items, _, _ = self._read_turns(reader, str(identity), start, end)
            return {
                "items": items,
                "next_before": start if start else None,
            }

    @stored
    def accept(self, identity: UUID, request: SendInput) -> tuple[Operation, bool]:
        fingerprint = hashlib.sha256(
            (str(identity) + request.model_dump_json()).encode()
        ).hexdigest()
        with self.database.transaction() as connection:
            if connection.execute(
                select(command_receipts.c.id).where(
                    command_receipts.c.id == str(request.operation_id)
                )
            ).first():
                raise conflict()
            previous = (
                connection.execute(
                    select(operations).where(operations.c.id == str(request.operation_id))
                )
                .mappings()
                .one_or_none()
            )
            if previous is not None:
                if previous["conversation_id"] != str(
                    identity
                ) or RecordedTurnRequest.model_validate_json(previous["request"]).model_dump(
                    include=set(SendInput.model_fields), mode="json"
                ) != request.model_dump(mode="json"):
                    raise conflict()
                return self._operation(dict(previous)), False
            conversation = self._get(connection, identity, with_turns=False)
            updated = accept_input(conversation, request)
            if connection.execute(
                select(operations.c.id).where(operations.c.active_slot == 1)
            ).first():
                raise KomaquillError(code="operation_busy", stage="operation", retryable=True)
            if updated is not conversation:
                self._store(connection, updated)
            conversation = updated
            enabled = self.database.content.capture(request.operation_id, connection)
            result = Operation(
                id=request.operation_id,
                conversation_id=identity,
                state="accepted",
                response_locale=request.locale or conversation.definition.locale,
                nsfw_enabled=enabled,
            )
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

    @stored
    def dismiss_proposal(
        self, identity: UUID, proposal_id: UUID, request: ProposalDismissal
    ) -> Conversation:
        with self.database.transaction() as connection:
            self._known_command(connection, identity, f"dismiss:{proposal_id}", request)
            conversation = self._get(connection, identity, with_turns=False, with_events=True)
            payload = self._event_payload(connection, identity, "proposals", proposal_id)
            proposed = ProposedAction.model_validate_json(payload) if payload is not None else None
            if (
                proposed is not None
                and proposed.state == "dismissed"
                and proposed.decision_id == request.operation_id
            ):
                return conversation
            if (
                proposed is None
                or proposed.state != "pending"
                or conversation.revision != request.expected_revision
            ):
                raise conflict()
            updated = conversation.model_copy(
                update={
                    "proposals": [
                        p.model_copy(
                            update={"state": "dismissed", "decision_id": request.operation_id}
                        )
                        if p.id == proposal_id
                        else p
                        for p in conversation.proposals
                    ]
                }
            )
            self._store(connection, updated)
            self._register_command(connection, identity, f"dismiss:{proposal_id}", request)
            connection.commit()
            return updated

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
        request = RecordedTurnRequest.model_validate_json(str(row["request"]))
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

    def _meeting(self, connection: Connection, conversation: Conversation) -> MeetingPlan | None:
        if not conversation.turn_count:
            return None
        turns, _, _ = self._read_turns(
            connection, str(conversation.id), conversation.turn_count - 1, conversation.turn_count
        )
        return turns[0].meeting_state

    @stored
    def request(self, identity: UUID) -> tuple[Conversation, TurnRequest]:
        with self.database.engine.connect() as connection:
            row = self._row(connection, identity)
            header = self._get(connection, UUID(str(row["conversation_id"])), with_turns=False)
            start = (
                header.memory.through_turn
                if header.memory and header.memory.source_version == 5
                else 0
            )
            request = RecordedTurnRequest.model_validate_json(str(row["request"]))
            request = self._bind_action(connection, header.id, request)
            if header.memory and header.memory.source_version == 5:
                for passage in header.memory.passages:
                    position = connection.execute(
                        select(conversation_turns.c.position).where(
                            conversation_turns.c.conversation_id == str(header.id),
                            conversation_turns.c.turn_id == passage.turn_id,
                        )
                    ).scalar_one_or_none()
                    if position is None or position >= header.memory.through_turn:
                        raise ValueError("Memory references an unavailable historical turn.")
                    turns, _, _ = self._read_turns(
                        connection, str(header.id), position, position + 1
                    )
                    if passage.quote not in memory_sources(turns[0]).get(passage.field, ""):
                        raise ValueError("Memory passage differs from its retained source.")
            value = self._get(
                connection, UUID(str(row["conversation_id"])), from_turn=start, with_events=False
            )
            if request.action_id is not None:
                payload: str = connection.execute(
                    select(conversation_events.c.payload).where(
                        conversation_events.c.conversation_id == str(value.id),
                        conversation_events.c.kind == "actions",
                        conversation_events.c.id == str(request.action_id),
                    )
                ).scalar_one()
                value = value.model_copy(update={"actions": [read_action(payload)]})
            return value, TurnRequest.model_validate_json(
                request.model_copy(
                    update={
                        "locale": self._operation(row).response_locale
                        or request.locale
                        or header.definition.locale,
                        "meeting_state": self._meeting(connection, header),
                    }
                ).model_dump_json(include=set(TurnRequest.model_fields))
            )

    @stored
    def operation(self, identity: UUID) -> Operation:
        self.reconcile()
        return self.operation_snapshot(identity)

    @stored
    def operation_snapshot(self, identity: UUID) -> Operation:
        with self.database.engine.connect() as connection:
            return Operation.model_validate_json(str(self._row(connection, identity)["payload"]))

    @stored
    def accepted_input(self, identity: UUID) -> AcceptedInput:
        with self.database.engine.connect() as connection:
            row = self._row(connection, identity)
            request = RecordedTurnRequest.model_validate_json(str(row["request"]))
            request = self._bind_action(connection, UUID(str(row["conversation_id"])), request)
            action = request.action
            if action is None and request.action_id is not None:
                payload: str = connection.execute(
                    select(conversation_events.c.payload).where(
                        conversation_events.c.conversation_id == str(row["conversation_id"]),
                        conversation_events.c.kind == "actions",
                        conversation_events.c.id == str(request.action_id),
                    )
                ).scalar_one()
                action = read_action(payload).request.action
            return AcceptedInput(text=request.text, action=action)

    @stored
    def operations(self, identity: UUID) -> builtins.list[Operation]:
        with self.database.engine.connect() as connection:
            self._get(connection, identity, with_turns=False)
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
        with self.database.engine.connect() as connection:
            row = self._row(connection, identity)
            if self._operation(row).state != "running":
                raise conflict()
        self.database.checkpoints.save(
            f"reply:{identity}", reply.model_dump(mode="json", exclude_unset=True)
        )

    @stored
    def commit(self, identity: UUID, reply: Reply) -> Operation:
        with self.database.transaction() as connection:
            result = self._commit(connection, identity, reply)
            connection.commit()
            return result

    def _commit(self, connection: Connection, identity: UUID, reply: Reply) -> Operation:
        row = self._row(connection, identity)
        operation = self._operation(row)
        conversation = self._get(connection, operation.conversation_id, with_turns=False)
        if operation.state != "running" or row["base_revision"] != conversation.revision:
            raise conflict()
        request = RecordedTurnRequest.model_validate_json(str(row["request"]))
        request = self._bind_action(connection, conversation.id, request)
        state = apply_reply(conversation, reply, action_confirmed=request.action_id is not None)
        action_payload = (
            connection.execute(
                select(conversation_events.c.payload).where(
                    conversation_events.c.conversation_id == str(conversation.id),
                    conversation_events.c.kind == "actions",
                    conversation_events.c.id == str(request.action_id),
                )
            ).scalar_one_or_none()
            if request.action_id is not None
            else None
        )
        action = read_action(action_payload) if action_payload else None
        meeting = self._meeting(connection, conversation)
        if reply.meeting_change is not None:
            try:
                quote = reply.meeting_change.quote
                if action and action.source and quote not in action.source.remaining(request.text):
                    raise ValueError("The meeting decision must be in the remaining message.")
                meeting = reply.meeting_change.apply(meeting, request.text, identity)
            except ValueError:
                raise KomaquillError(code="invalid_response", retryable=False) from None
        answer = None
        if reply.meeting_query:
            answer = meeting.answer() if meeting else MeetingAnswer(status="unknown")
        turn = Turn(
            operation_id=identity,
            action=action.request.action if action else None,
            user=request.text,
            assistant=reply.text,
            narration="" if reply.action_proposals else (state.scenery or state.place),
            state=state,
            action_narration=reply.action_narration,
            notes=conversation.notes,
            action_id=request.action_id,
            meeting=answer,
            meeting_state=meeting,
        )
        updated = conversation.model_copy(
            update={
                "state": state,
                "turns": [*conversation.turns, turn],
                "turn_count": conversation.turn_count + 1,
                "turn_offset": conversation.turn_count,
                "revision": conversation.revision + 1,
                "proposals": [
                    p.model_copy(update={"state": "expired"}) if p.state == "pending" else p
                    for p in conversation.proposals
                ]
                + [
                    ProposedAction(
                        id=uuid4(),
                        turn_id=identity,
                        revision=conversation.revision + 1,
                        action=action,
                    )
                    for action in reply.action_proposals
                ],
            }
        )
        self._store(connection, updated)
        result = operation.model_copy(update={"state": "complete", "image_state": "disabled"})
        self._save(connection, result)
        return result

    def _received_calls(self, identity: UUID) -> bool:
        try:
            from komaquill.adapters.persistence.artifacts import ArtifactRepository

            calls = [value for _, _, value in ArtifactRepository(self.database).calls(identity)]
            return bool(calls) and all(
                isinstance(item, dict)
                and item.get("status") == "response_received"
                and isinstance(item.get("response"), str)
                and isinstance(item.get("invocation_id"), str)
                and bool(item["invocation_id"])
                and type(item.get("attempt")) is int
                and item["attempt"] >= 0
                and isinstance(item.get("messages"), list)
                for item in calls
            )
        except (OSError, ValueError, KomaquillError):
            return False

    @stored
    def fail(
        self, identity: UUID, code: str, stage: OperationStage = OperationStage.OPERATION
    ) -> Operation:
        resumable = code in {"storage_failed", "operation_failed"} and self._received_calls(
            identity
        )
        with self.database.transaction() as connection:
            result = Operation.model_validate_json(str(self._row(connection, identity)["payload"]))
            if result.state in {"accepted", "running"}:
                result = result.model_copy(
                    update={
                        "state": "interrupted" if resumable else "failed",
                        "error": code,
                        "error_stage": stage,
                        "can_resume": resumable,
                    }
                )
                self._save(connection, result)
            connection.commit()
            return result

    @stored
    def save_memory(self, identity: UUID, memory: ConversationMemory) -> None:
        with self.database.transaction() as connection:
            conversation = self._get(connection, identity, with_turns=False)
            if memory.through_turn > conversation.turn_count:
                raise conflict()
            if conversation.memory and conversation.memory.through_turn >= memory.through_turn:
                return
            value = conversation.model_copy(update={"memory": memory})
            self._store(connection, value)
            connection.commit()

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
    def reconcile(self) -> None:
        retained: dict[str, Reply | None] = {}
        retention_errors: dict[str, str] = {}
        with self.database.engine.connect() as reader:
            identities: builtins.list[str] = list(
                reader.execute(
                    select(operations.c.id).where(operations.c.active_slot == 1)
                ).scalars()
            )
        for identity in identities:
            try:
                saved = self.database.checkpoints.load(f"reply:{identity}")
                if saved is None:
                    saved = self.database.checkpoints.conversation_result(
                        f"conversation:{identity}:move-response"
                    )
                if saved is None:
                    saved = self.database.checkpoints.conversation_result(
                        f"conversation:{identity}"
                    )
                candidate = TURN_RESULT.validate_python(saved) if saved is not None else None
                retained[identity] = candidate if isinstance(candidate, Reply) else None
            except KomaquillError as error:
                retention_errors[identity] = error.code.value
            except (OSError, ValueError):
                retention_errors[identity] = "storage_failed"
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
                if value.state == "running":
                    with self.database.execution_lock(value.id) as available:
                        if not available:
                            continue
                        alive = False
                if not alive:
                    if str(value.id) in retention_errors:
                        self._save(
                            connection,
                            value.model_copy(
                                update={
                                    "state": "interrupted",
                                    "error": retention_errors[str(value.id)],
                                    "can_resume": False,
                                }
                            ),
                        )
                        continue
                    if value.state == "running" and retained.get(str(value.id)) is not None:
                        try:
                            saved_reply = retained[str(value.id)]
                            assert saved_reply is not None
                            value = self._commit(connection, value.id, saved_reply)
                        except KomaquillError as error:
                            if error.code not in {"operation_conflict", "invalid_response"}:
                                raise
                            value = value.model_copy(
                                update={
                                    "state": "interrupted",
                                    "error": error.code.value,
                                    "can_resume": False,
                                }
                            )
                        if value.image_state != "pending":
                            self._save(connection, value)
                            continue
                    changes: dict[str, object] = (
                        {"state": "interrupted", "error": "submission_unknown"}
                        if value.state in {"accepted", "running"}
                        else {"image_state": "failed", "image_error": "submission_unknown"}
                    )
                    if value.state in {"accepted", "running"}:
                        safe = self._received_calls(value.id)
                        changes["can_resume"] = safe
                        if safe:
                            changes["error"] = "operation_failed"
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

    @stored
    def save_notes(self, identity: UUID, request: NotesRequest) -> Conversation:
        with self.database.transaction() as connection:
            known = self._known_command(connection, identity, "notes", request)
            value = self._get(connection, identity, with_turns=False)
            if known:
                return value
            if (
                value.revision != request.expected_revision
                or connection.execute(
                    select(operations.c.id).where(operations.c.active_slot == 1)
                ).first()
            ):
                raise conflict()
            value = value.model_copy(
                update={
                    "notes": request.notes,
                    "revision": value.revision + 1,
                    "proposals": [
                        p.model_copy(update={"state": "expired"}) if p.state == "pending" else p
                        for p in value.proposals
                    ],
                }
            )
            self._store(connection, value)
            self._register_command(connection, identity, "notes", request)
            connection.commit()
            return value

    @stored
    def prepare_resume(self, identity: UUID, allow_real_llm: bool) -> Operation:
        with self.database.transaction() as connection:
            row = self._row(connection, identity)
            result = self._operation(row)
            if result.state == "complete":
                return result
            if not result.can_resume or result.state != "interrupted":
                raise KomaquillError(code="submission_unknown", retryable=False)
            conversation = self._get(connection, result.conversation_id, with_turns=False)
            if (
                row["base_revision"] != conversation.revision
                or connection.execute(
                    select(operations.c.id).where(operations.c.active_slot == 1)
                ).first()
            ):
                raise conflict()
            request = RecordedTurnRequest.model_validate_json(str(row["request"]))
            if request.allow_real_llm and not allow_real_llm:
                raise KomaquillError(code="real_llm_not_allowed", retryable=False)
            result = result.model_copy(
                update={
                    "state": "accepted",
                    "error": None,
                    "error_stage": None,
                    "can_resume": False,
                    "resume_count": result.resume_count + 1,
                }
            )
            self._save(connection, result)
            connection.execute(
                operations.update()
                .where(operations.c.id == str(identity))
                .values(owner_pid=os.getpid(), owner_started=str(psutil.Process().create_time()))
            )
            connection.commit()
            return result

    @stored
    def bookmarks(self, identity: UUID) -> builtins.list[Bookmark]:
        with self.database.engine.connect() as reader:
            return [
                read_bookmark(str(row[0]))
                for row in reader.execute(
                    select(conversation_bookmarks.c.payload).where(
                        conversation_bookmarks.c.conversation_id == str(identity)
                    )
                )
            ]

    @stored
    def bookmark(self, identity: UUID, request: SceneRequest) -> Bookmark:
        with self.database.transaction() as connection:
            known = self._known_command(connection, identity, "bookmark", request)
            if known:
                return read_bookmark(
                    connection.execute(
                        select(conversation_bookmarks.c.payload).where(
                            conversation_bookmarks.c.id == str(request.operation_id)
                        )
                    ).scalar_one()
                )
            value = self._get(connection, identity, with_turns=False)
            if value.revision != request.expected_revision:
                raise conflict()
            payload = connection.execute(
                select(conversation_turns.c.payload).where(
                    conversation_turns.c.conversation_id == str(identity),
                    conversation_turns.c.turn_id == str(request.turn_id),
                )
            ).scalar_one_or_none()
            if payload is None:
                raise missing()
            turn = read_turn(json.loads(payload))
            result = Bookmark(
                id=request.operation_id,
                conversation_id=identity,
                turn_id=request.turn_id,
                name=request.name,
                state=turn.state,
                notes=turn.notes,
            )
            connection.execute(
                conversation_bookmarks.insert().values(
                    id=str(result.id),
                    conversation_id=str(identity),
                    payload=result.model_dump_json(),
                )
            )
            self._register_command(connection, identity, "bookmark", request)
            connection.commit()
            return result

    @stored
    def fork(self, identity: UUID, request: SceneRequest) -> Conversation:
        with self.database.transaction() as connection:
            known = self._known_command(connection, identity, "fork", request)
            if known:
                return self._get(connection, request.operation_id)
            value = self._get(connection, identity)
            if value.revision != request.expected_revision:
                raise conflict()
            index = next(
                (i for i, turn in enumerate(value.turns) if turn.operation_id == request.turn_id),
                None,
            )
            if index is None:
                raise missing()
            turn = value.turns[index]
            inherited = value.turns[: index + 1]
            retained = {item.operation_id for item in inherited}
            result = Conversation(
                id=request.operation_id,
                definition=value.definition.model_copy(update={"title": request.name}),
                state=turn.state,
                notes=turn.notes,
                turns=inherited,
                turn_count=len(inherited),
                actions=[a for a in value.actions if a.reaction_id in retained],
                source_conversation_id=identity,
                source_turn_id=turn.operation_id,
            )
            self._store(connection, result, create=True)
            self._register_command(connection, identity, "fork", request)
            connection.commit()
            return result

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
