"""Apply permitted companion changes while protecting confirmed player facts."""

from komaquill.domain.conversations.models import Conversation, Reply
from komaquill.domain.errors import KomaquillError
from komaquill.domain.roleplay import ConversationState


def require_visible_reply(
    before: ConversationState, state: ConversationState, text: str, action_narration: str
) -> None:
    content = bool(text.strip() or action_narration.strip())
    present = state.character_place == state.place
    new_scenery = bool(state.scenery.strip() and state.scenery != before.scenery)
    if (not present and content) or (present and not content and not new_scenery):
        raise KomaquillError(code="invalid_response", retryable=False)


def apply_reply(conversation: Conversation, reply: Reply) -> ConversationState:
    before = conversation.state
    state = reply.state_patch.apply(before)
    if not reply.action_proposals:
        require_visible_reply(before, state, reply.text, reply.action_narration)
    if (
        (
            reply.action_proposals
            and (reply.text or reply.narration or reply.action_narration or state != before)
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
