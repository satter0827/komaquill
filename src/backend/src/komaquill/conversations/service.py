"""Commit validated turns before dispatching independent image work."""

from __future__ import annotations

import builtins
from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from komaquill.conversations.actions import ActionRecord, ActionRequest, ProposalDismissal
from komaquill.conversations.models import (
    ConnectionProfile,
    Conversation,
    ConversationInput,
    Operation,
    Reply,
    SendInput,
)
from komaquill.conversations.transitions import apply_reply, published_reply
from komaquill.conversations.visuals import VisualReview
from komaquill.domain.errors import KomaquillError


class Repository(Protocol):
    def create(self, definition: ConversationInput) -> Conversation: ...
    def list(self) -> list[Conversation]: ...
    def get(self, identity: UUID) -> Conversation: ...
    def accept(self, identity: UUID, request: SendInput) -> tuple[Operation, bool]: ...
    def request(self, identity: UUID) -> tuple[Conversation, SendInput]: ...
    def operation(self, identity: UUID) -> Operation: ...
    def execution(self, identity: UUID) -> AbstractContextManager[bool]: ...
    def commit(self, identity: UUID, reply: Reply) -> Operation: ...
    def checkpoint(self, identity: UUID, reply: Reply) -> None: ...
    def fail(self, identity: UUID, code: str) -> Operation: ...
    def image(self, identity: UUID, image_id: UUID | None, error: str | None = None) -> None: ...
    def reconcile(self) -> None: ...
    def accept_action(
        self, identity: UUID, request: ActionRequest
    ) -> tuple[ActionRecord, bool]: ...
    def dismiss_proposal(
        self, identity: UUID, proposal_id: UUID, request: ProposalDismissal
    ) -> Conversation: ...
    def review_visuals(self, identity: UUID, review: VisualReview) -> Conversation: ...
    def operations(self, identity: UUID) -> builtins.list[Operation]: ...


class Responder(Protocol):
    def reply(self, conversation: Conversation, request: SendInput) -> Reply: ...
    def profiles(self) -> list[ConnectionProfile]: ...


class ConversationService:
    def __init__(self, repository: Repository, responder: Responder) -> None:
        self.repository = repository
        self.responder = responder

    def create(self, definition: ConversationInput) -> Conversation:
        if definition.llm_profile not in {profile.id for profile in self.profiles()}:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        initial = definition.initial_state
        resolved = initial.model_copy(
            update={
                field: getattr(definition.scenario, field)
                for field in ("place", "goal")
                if field not in initial.model_fields_set
            }
        )
        resolved = resolved.model_copy(
            update={
                "character_place": resolved.character_place
                if "character_place" in initial.model_fields_set
                else resolved.place,
                **{
                    role: getattr(resolved, role).model_copy(
                        update={
                            **(
                                {"clothing": person.initial_clothing}
                                if getattr(resolved, role).clothing is None
                                else {}
                            ),
                            **(
                                {"held_objects": person.initial_held_objects}
                                if getattr(resolved, role).held_objects is None
                                and person.initial_held_objects
                                else {}
                            ),
                        }
                    )
                    for role, person in (
                        ("player", definition.user),
                        ("character", definition.character),
                    )
                },
            }
        )
        return self.repository.create(definition.model_copy(update={"initial_state": resolved}))

    def profiles(self) -> list[ConnectionProfile]:
        return self.responder.profiles()

    def list(self) -> list[Conversation]:
        return self.repository.list()

    def get(self, identity: UUID) -> Conversation:
        return self.repository.get(identity)

    def status(self, identity: UUID) -> Conversation:
        return self.get(identity)

    def operations(self, identity: UUID) -> builtins.list[Operation]:
        self.repository.reconcile()
        return self.repository.operations(identity)

    def accept(self, identity: UUID, request: SendInput) -> tuple[Operation, bool]:
        self.repository.reconcile()
        return self.repository.accept(identity, request)

    def execute(self, identity: UUID) -> Operation:
        with self.repository.execution(identity) as claimed:
            if not claimed:
                return self.repository.operation(identity)
            try:
                conversation, request = self.repository.request(identity)
                reply = published_reply(conversation, self.responder.reply(conversation, request))
                apply_reply(conversation, reply)
            except KomaquillError as error:
                return self.repository.fail(identity, error.code.value)
            except Exception:
                return self.repository.fail(identity, "operation_failed")
            self.repository.checkpoint(identity, reply)
            self.repository.commit(identity, reply)
            return self.repository.operation(identity)

    def send(self, identity: UUID, request: SendInput) -> Operation:
        operation, created = self.accept(identity, request)
        return self.execute(operation.id) if created else operation

    def accept_action(self, identity: UUID, request: ActionRequest) -> tuple[ActionRecord, bool]:
        self.repository.reconcile()
        return self.repository.accept_action(identity, request)

    def act(self, identity: UUID, request: ActionRequest) -> ActionRecord:
        record, created = self.accept_action(identity, request)
        if created:
            self.execute(record.reaction_id)
        return record

    def actions(self, identity: UUID) -> builtins.list[ActionRecord]:
        return self.get(identity).actions

    def dismiss_proposal(
        self, identity: UUID, proposal_id: UUID, request: ProposalDismissal
    ) -> Conversation:
        return self.repository.dismiss_proposal(identity, proposal_id, request)

    def review_visuals(self, identity: UUID, review: VisualReview) -> Conversation:
        return self.repository.review_visuals(identity, review)

    def resume(self, identity: UUID) -> Conversation:
        self.repository.reconcile()
        return self.get(identity)
