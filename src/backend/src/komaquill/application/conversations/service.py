"""Accept messages and player actions atomically before generating a reply."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from komaquill.domain.conversations.actions import ActionRecord, ProposalDismissal
from komaquill.domain.conversations.editing import Bookmark, NotesRequest, SceneRequest
from komaquill.domain.conversations.models import (
    AcceptedInput,
    ConnectionProfile,
    Conversation,
    ConversationInput,
    Operation,
    Reply,
    SendInput,
    TurnRequest,
)
from komaquill.domain.conversations.transitions import apply_reply
from komaquill.domain.conversations.visuals import VisualReview
from komaquill.domain.errors import FailureInfo, KomaquillError, OperationStage, failure_info


class Repository(Protocol):
    def create(self, definition: ConversationInput) -> Conversation: ...
    def list(self) -> list[Conversation]: ...
    def get(self, identity: UUID) -> Conversation: ...
    def overview(self, identity: UUID) -> Conversation: ...
    def prepare_resume(self, identity: UUID, allow_real_llm: bool) -> Operation: ...
    def turns(
        self, identity: UUID, before: int | None = None, limit: int = 30
    ) -> dict[str, object]: ...
    def save_notes(self, identity: UUID, request: NotesRequest) -> Conversation: ...
    def bookmark(self, identity: UUID, request: SceneRequest) -> Bookmark: ...
    def bookmarks(self, identity: UUID) -> builtins.list[Bookmark]: ...
    def fork(self, identity: UUID, request: SceneRequest) -> Conversation: ...
    def accept(self, identity: UUID, request: SendInput) -> tuple[Operation, bool]: ...
    def request(self, identity: UUID) -> tuple[Conversation, TurnRequest]: ...
    def operation(self, identity: UUID) -> Operation: ...
    def operation_snapshot(self, identity: UUID) -> Operation: ...
    def accepted_input(self, identity: UUID) -> AcceptedInput: ...
    def execution(self, identity: UUID) -> AbstractContextManager[bool]: ...
    def commit(self, identity: UUID, reply: Reply) -> Operation: ...
    def checkpoint(self, identity: UUID, reply: Reply) -> None: ...
    def fail(
        self, identity: UUID, code: str, stage: OperationStage = OperationStage.OPERATION
    ) -> Operation: ...
    def image(self, identity: UUID, image_id: UUID | None, error: str | None = None) -> None: ...
    def reconcile(self) -> None: ...
    def dismiss_proposal(
        self, identity: UUID, proposal_id: UUID, request: ProposalDismissal
    ) -> Conversation: ...
    def review_visuals(self, identity: UUID, review: VisualReview) -> Conversation: ...
    def operations(self, identity: UUID) -> builtins.list[Operation]: ...
    def actions(self, identity: UUID) -> builtins.list[ActionRecord]: ...


class Responder(Protocol):
    def reply(self, conversation: Conversation, request: TurnRequest) -> Reply: ...
    def profiles(self) -> list[ConnectionProfile]: ...


class ConversationService:
    def resume_operation(
        self, identity: UUID, *, allow_real_llm: bool = False, execute: bool = True
    ) -> Operation:
        result = self.repository.prepare_resume(identity, allow_real_llm)
        return self.execute(identity) if execute and result.state == "accepted" else result

    def overview(self, identity: UUID) -> Conversation:
        return self.repository.overview(identity)

    def turns(
        self, identity: UUID, before: int | None = None, limit: int = 30
    ) -> dict[str, object]:
        return self.repository.turns(identity, before, limit)

    def save_notes(self, identity: UUID, request: NotesRequest) -> Conversation:
        return self.repository.save_notes(identity, request)

    def bookmark(self, identity: UUID, request: SceneRequest) -> Bookmark:
        return self.repository.bookmark(identity, request)

    def bookmarks(self, identity: UUID) -> list[Bookmark]:
        return self.repository.bookmarks(identity)

    def fork(self, identity: UUID, request: SceneRequest) -> Conversation:
        return self.repository.fork(identity, request)

    def __init__(
        self,
        repository: Repository,
        responder: Responder,
        preflight: Callable[[UUID, bool], None],
        report_failure: Callable[[UUID, FailureInfo], object] | None = None,
    ) -> None:
        self.preflight = preflight
        self.repository = repository
        self.responder = responder
        self.report_failure = report_failure

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
        try:
            self.repository.operation(request.operation_id)
        except KomaquillError as error:
            if error.code != "request_not_found":
                raise
            self.preflight(identity, request.allow_real_llm)
        return self.repository.accept(identity, request)

    def execute(self, identity: UUID) -> Operation:
        with self.repository.execution(identity) as claimed:
            if not claimed:
                return self.repository.operation(identity)
            try:
                conversation, request = self.repository.request(identity)
                reply = self.responder.reply(conversation, request)
                apply_reply(conversation, reply)
            except KomaquillError as error:
                if self.report_failure:
                    self.report_failure(identity, failure_info(error, "operation"))
                return self.repository.fail(identity, error.code.value, error.stage)
            except Exception as error:
                if self.report_failure:
                    self.report_failure(identity, failure_info(error, "operation"))
                return self.repository.fail(identity, "operation_failed")
            self.repository.checkpoint(identity, reply)
            self.repository.commit(identity, reply)
            return self.repository.operation(identity)

    def send(self, identity: UUID, request: SendInput) -> Operation:
        operation, created = self.accept(identity, request)
        return self.execute(operation.id) if created else operation

    def actions(self, identity: UUID) -> builtins.list[ActionRecord]:
        return self.repository.actions(identity)

    def dismiss_proposal(
        self, identity: UUID, proposal_id: UUID, request: ProposalDismissal
    ) -> Conversation:
        return self.repository.dismiss_proposal(identity, proposal_id, request)

    def review_visuals(self, identity: UUID, review: VisualReview) -> Conversation:
        return self.repository.review_visuals(identity, review)

    def resume(self, identity: UUID) -> Conversation:
        self.repository.reconcile()
        return self.get(identity)
