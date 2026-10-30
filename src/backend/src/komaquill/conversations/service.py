"""Commit validated turns before dispatching independent image work."""

from __future__ import annotations

import builtins
from typing import Protocol
from uuid import UUID

from komaquill.conversations.models import (
    ConnectionProfile,
    Conversation,
    ConversationInput,
    Operation,
    Reply,
    SendInput,
    StateCommand,
)
from komaquill.domain.errors import KomaquillError


class Repository(Protocol):
    def create(self, definition: ConversationInput) -> Conversation: ...
    def list(self) -> list[Conversation]: ...
    def get(self, identity: UUID) -> Conversation: ...
    def accept(self, identity: UUID, request: SendInput) -> tuple[Operation, bool]: ...
    def request(self, identity: UUID) -> tuple[Conversation, SendInput]: ...
    def operation(self, identity: UUID) -> Operation: ...
    def running(self, identity: UUID) -> bool: ...
    def commit(self, identity: UUID, reply: Reply) -> Operation: ...
    def checkpoint(self, identity: UUID, reply: Reply) -> None: ...
    def fail(self, identity: UUID, code: str) -> Operation: ...
    def image(self, identity: UUID, image_id: UUID | None, error: str | None = None) -> None: ...
    def command(self, identity: UUID, command: StateCommand) -> Conversation: ...
    def reconcile(self) -> None: ...
    def operations(self, identity: UUID) -> builtins.list[Operation]: ...


class Responder(Protocol):
    def reply(self, conversation: Conversation, request: SendInput) -> Reply: ...
    def profiles(self) -> list[ConnectionProfile]: ...


class Images(Protocol):
    def submit(self, prompt: str, *, run_id: UUID | None = None) -> object: ...


class ConversationService:
    def __init__(self, repository: Repository, responder: Responder, images: Images) -> None:
        self.repository = repository
        self.responder = responder
        self.images = images

    def create(self, definition: ConversationInput) -> Conversation:
        if definition.llm_profile not in {profile.id for profile in self.profiles()}:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        return self.repository.create(definition)

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
        if not self.repository.running(identity):
            return self.repository.operation(identity)
        try:
            conversation, request = self.repository.request(identity)
            reply = self.responder.reply(conversation, request)
        except KomaquillError as error:
            return self.repository.fail(identity, error.code.value)
        except Exception:
            return self.repository.fail(identity, "operation_failed")
        self.repository.checkpoint(identity, reply)
        operation = self.repository.commit(identity, reply)
        if operation.image_state == "pending" and reply.scene:
            try:
                self.images.submit(reply.scene, run_id=identity)
                self.repository.image(identity, identity)
            except KomaquillError as error:
                self.repository.image(identity, None, error.code.value)
            except Exception:
                self.repository.image(identity, None, "operation_failed")
        return self.repository.operation(identity)

    def send(self, identity: UUID, request: SendInput) -> Operation:
        operation, created = self.accept(identity, request)
        return self.execute(operation.id) if created else operation

    def command(self, identity: UUID, command: StateCommand) -> Conversation:
        self.repository.reconcile()
        return self.repository.command(identity, command)

    def resume(self, identity: UUID) -> Conversation:
        self.repository.reconcile()
        return self.get(identity)
