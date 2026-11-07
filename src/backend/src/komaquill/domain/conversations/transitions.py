"""Apply permitted companion changes while protecting confirmed player facts."""

from komaquill.domain.conversations.actions import MoveAction
from komaquill.domain.conversations.models import Conversation, Reply
from komaquill.domain.errors import KomaquillError
from komaquill.domain.roleplay import ConversationState


def require_visible_reply(
    before: ConversationState,
    state: ConversationState,
    text: str,
    action_narration: str,
    *,
    meeting_query: bool = False,
) -> None:
    content = bool(text.strip() or action_narration.strip() or meeting_query)
    new_scenery = bool(state.scenery.strip() and state.scenery != before.scenery)
    if not content and not new_scenery:
        raise KomaquillError(code="invalid_response", retryable=False)


def apply_reply(
    conversation: Conversation, reply: Reply, *, action_confirmed: bool = False
) -> ConversationState:
    before = conversation.state
    state = reply.state_patch.apply(before)
    if not reply.action_proposals:
        require_visible_reply(
            before, state, reply.text, reply.action_narration, meeting_query=reply.meeting_query
        )
    if (
        (
            reply.action_proposals
            and (
                action_confirmed
                or any(isinstance(action, MoveAction) for action in reply.action_proposals)
                or reply.text
                or reply.narration
                or reply.action_narration
                or reply.meeting_change is not None
                or reply.meeting_query
                or state != before
            )
        )
        or state.player != before.player
        or state.place != before.place
        or state.goal != before.goal
        or state.time != before.time
        or state.character.appearance != before.character.appearance
        or {item.id for item in state.character.held_objects or []}.intersection(
            item.id for item in state.player.held_objects or []
        )
    ):
        raise KomaquillError(code="invalid_response", retryable=False)
    return state
