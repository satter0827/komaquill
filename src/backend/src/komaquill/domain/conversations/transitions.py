"""Apply permitted companion changes while protecting confirmed player facts."""

from komaquill.domain.conversations.models import Conversation, Reply
from komaquill.domain.errors import KomaquillError
from komaquill.domain.roleplay import ConversationState, StatePatch


def published_reply(conversation: Conversation, reply: Reply) -> Reply:
    if reply.action_proposals:
        return reply.model_copy(
            update={
                "text": "",
                "narration": "",
                "action_narration": "",
                "state_patch": StatePatch(),
            }
        )
    state = reply.state_patch.apply(conversation.state)
    if state.character_place != state.place:
        return reply.model_copy(
            update={"text": "", "action_narration": "", "narration": state.scenery or state.place}
        )
    return reply.model_copy(update={"narration": state.scenery or state.place})


def apply_reply(conversation: Conversation, reply: Reply) -> ConversationState:
    before = conversation.state
    state = reply.state_patch.apply(before)
    if (
        state.player != before.player
        or state.place != before.place
        or state.goal != before.goal
        or state.time != before.time
        or state.character.appearance != before.character.appearance
        or (state.character_place != state.place and reply.text)
        or {item.id for item in state.character.held_objects or []}.intersection(
            item.id for item in state.player.held_objects or []
        )
    ):
        raise KomaquillError(code="invalid_response", retryable=False)
    return state
