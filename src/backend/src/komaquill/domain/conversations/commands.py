"""Validate and apply accepted player input independently of persistence."""

from komaquill.domain.conversations.actions import ActionRecord, RecordedActionRequest, apply_action
from komaquill.domain.conversations.models import Conversation, SendInput
from komaquill.domain.errors import KomaquillError


def accept_input(conversation: Conversation, request: SendInput) -> Conversation:
    if conversation.revision != request.expected_revision:
        raise KomaquillError(code="operation_conflict", stage="operation", retryable=False)
    if request.action is None:
        return conversation
    proposals = list(conversation.proposals)
    if request.proposal_id is not None:
        proposed = next((value for value in proposals if value.id == request.proposal_id), None)
        if (
            proposed is None
            or proposed.state != "pending"
            or proposed.revision != conversation.revision
            or proposed.action != request.action
        ):
            raise KomaquillError(code="operation_conflict", stage="operation", retryable=False)
        proposals = [
            value.model_copy(update={"state": "applied", "decision_id": request.operation_id})
            if value.id == proposed.id
            else value
            for value in proposals
        ]
    state = apply_action(conversation.state, request.action)
    record = ActionRecord(
        request=RecordedActionRequest(
            **{
                **request.model_dump(exclude={"text"}),
                "locale": request.locale or conversation.definition.locale,
            }
        ),
        before=conversation.state,
        after=state,
        revision=conversation.revision + 1,
        reaction_id=request.operation_id,
    )
    return conversation.model_copy(
        update={
            "state": state,
            "revision": record.revision,
            "actions": [*conversation.actions, record],
            "proposals": [
                value.model_copy(update={"state": "expired"}) if value.state == "pending" else value
                for value in proposals
            ],
        }
    )
