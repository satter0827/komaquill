"""Compose conversation context and validate visible replies."""

import json
from collections.abc import Callable
from functools import cached_property
from importlib.resources import files
from typing import Annotated, Any, Literal, Self, cast
from uuid import UUID

from filelock import Timeout
from pydantic import Field, model_validator

from komaquill.adapters.llm.constraints import ResponseConstraintError
from komaquill.adapters.llm.execution import StructuredExecution, response_schema
from komaquill.adapters.llm.language import ResponseLanguageError, check_language
from komaquill.domain.conversations.actions import (
    InferredMove,
    IntentionAction,
    MoveAction,
    PrepareAction,
    WaitAction,
)
from komaquill.domain.conversations.context import scene_context
from komaquill.domain.conversations.evidence import SourceSpan
from komaquill.domain.conversations.meetings import MeetingChange
from komaquill.domain.conversations.models import (
    ConnectionProfile,
    Conversation,
    Reply,
    TurnRequest,
    TurnResult,
)
from komaquill.domain.conversations.transitions import require_visible_reply
from komaquill.domain.errors import KomaquillError
from komaquill.domain.library.models import CharacterInput, PlayerInput, StoryInput
from komaquill.domain.roleplay import (
    MAX_CLOTHING_LENGTH,
    MAX_EXPRESSION_POSE_LENGTH,
    MAX_RELATIONSHIP_LENGTH,
    MAX_SCENERY_LENGTH,
    ConversationMemory,
    ConversationState,
    PhysicalObjects,
    Record,
    StatePatch,
)


class ProposedMove(MoveAction):
    kind: Literal["move"]
    quote: str = Field(min_length=1, description="Verbatim immediate travel clause only.")


class ProposedWait(WaitAction):
    kind: Literal["wait"]


class ProposedPreparation(PrepareAction):
    """A physical self-action: changing clothes, pose, expression or held objects."""

    kind: Literal["prepare"]


class ProposedIntention(IntentionAction):
    """An explicitly adopted current personal goal, excluding questions and conditional plans."""

    kind: Literal["intention"]


GeneratedAction = Annotated[
    ProposedWait | ProposedPreparation | ProposedIntention,
    Field(discriminator="kind"),
]


class RelationshipChange(Record):
    field: Literal["relationship"]
    value: str = Field(max_length=MAX_RELATIONSHIP_LENGTH)


class SceneryChange(Record):
    field: Literal["scenery"]
    value: str = Field(max_length=MAX_SCENERY_LENGTH)


class ClothingChange(Record):
    field: Literal["clothing"]
    value: str = Field(
        max_length=MAX_CLOTHING_LENGTH,
        description=(
            "Only assistant_character's new clothing. Never copy human_player's clothing change. "
            "An empty string removes the companion's own clothing."
        ),
    )


class ExpressionPoseChange(Record):
    field: Literal["expression", "pose"]
    value: str = Field(max_length=MAX_EXPRESSION_POSE_LENGTH)


class ObjectsChange(Record):
    field: Literal["held_objects"]
    value: PhysicalObjects = Field(
        description=(
            "Emit this change only after an actual physical pickup, release or handover by "
            "assistant_character in this turn, explained in action_narration. The value is the "
            "complete inventory after that action, preserving other objects. Do not emit an "
            "inventory merely to answer information or recall questions, and never restore "
            "an earlier inventory. "
            "Passwords, titles and quoted words never grant possession of a physical object. "
            "A handover cannot originate from a person who does not hold that object."
        )
    )


SceneChange = Annotated[
    RelationshipChange | SceneryChange | ClothingChange | ExpressionPoseChange | ObjectsChange,
    Field(discriminator="field"),
]


class DialogueResult(Record):
    kind: Literal["companion"]
    meeting_change: MeetingChange | None = Field(
        description="The latest user's explicit rendezvous decision, or null."
    )
    meeting_query: Literal["none", "only", "mixed"] = Field(
        description="Whether the user asks to review the meeting alone or alongside other topics."
    )
    changes: list[SceneChange] = Field(
        max_length=6,
        description=(
            "Only actual new changes to assistant_character or the environment. "
            "A completed human_player action does not change the companion's clothing or "
            "objects. Both people are already at the confirmed shared place. "
            "Return [] when unchanged."
        ),
    )
    action_narration: str = Field(
        max_length=6000,
        description=(
            "Visible actions performed by assistant_character, consistent with their own clothing "
            "and held objects. Do not repeat human_player's completed action or assign their "
            "possessions to the companion. Discuss a suggested, scheduled or conditional action "
            "without performing it. Leave action_narration empty when only speaking."
        ),
    )
    text: str = Field(
        max_length=20000,
        description=(
            str(Reply.model_fields["text"].description)
            + " Answer the latest question directly before offering a follow-up question."
            " Keep response-framing JSON outside the dialogue; include literal JSON only when "
            "the player explicitly asks to discuss it. For a mixed meeting query, use text "
            "only for the separate questions or other confirmed decisions."
        ),
    )

    @model_validator(mode="after")
    def unique_changes(self) -> Self:
        if len({change.field for change in self.changes}) != len(self.changes):
            raise ValueError("A scene field may only change once.")
        return self


class ProposalResult(Record):
    kind: Literal["player"]
    action: GeneratedAction


class NarrativeReply(Record):
    response: Annotated[ProposalResult | DialogueResult, Field(discriminator="kind")]
    player_move: ProposedMove | None = Field(
        description=(
            "Required independent decision: the human player's explicit immediate destination, "
            "including movement combined with dialogue or a question. Both people move together. "
            "Null for questions about moving, conditional plans, future appointments or no move."
        )
    )

    @model_validator(mode="after")
    def one_player_action(self) -> Self:
        if self.player_move is not None and isinstance(self.response, ProposalResult):
            raise ValueError("A movement cannot include another player action proposal.")
        return self


class ReactionReply(Record):
    response: DialogueResult


class ArrivalDialogue(DialogueResult):
    action_narration: Literal[""]


class ArrivalReply(ReactionReply):
    response: ArrivalDialogue


def resolve_scene(conversation: Conversation, generated: DialogueResult) -> ConversationState:
    changes: dict[str, Any] = {}
    for change in generated.changes:
        if change.field in {"relationship", "scenery"}:
            changes[change.field] = change.value
        else:
            changes.setdefault("character", {})[change.field] = change.value
    return StatePatch.model_validate(changes).apply(conversation.state)


class ConversationResponder:
    @cached_property
    def graph(self) -> Any:
        from komaquill.adapters.llm.workflow import compile_conversation

        return compile_conversation(self.execution.checkpoints)

    def __init__(
        self,
        execution: StructuredExecution,
        save_memory: Callable[[UUID, ConversationMemory], None],
        read_history_hash: Callable[[UUID, int], str],
    ) -> None:
        self.execution = execution
        self.save_memory = save_memory
        self.read_history_hash = read_history_hash
        prompts = files("komaquill").joinpath("resources/conversations")
        self.instructions = prompts.joinpath("instructions.txt").read_text(encoding="utf-8")
        self.intent = prompts.joinpath("intent.txt").read_text(encoding="utf-8").strip()

    def profiles(self) -> list[ConnectionProfile]:
        return [
            ConnectionProfile(id=name, provider=p.provider, model=p.model)
            for name, p in self.execution.settings.llm_profiles.items()
        ]

    def reply(self, conversation: Conversation, request: TurnRequest) -> TurnResult:
        try:
            with self.execution.lock:
                from komaquill.adapters.llm.workflow import run_conversation

                return run_conversation(self.graph, conversation, request, self._reply)
        except Timeout:
            raise KomaquillError(code="operation_busy", retryable=True) from None
        except ImportError:
            raise KomaquillError(code="llm_dependencies_missing", retryable=False) from None

    def _reply(self, conversation: Conversation, request: TurnRequest) -> TurnResult:
        profile = self.execution.settings.llm_profiles.get(conversation.definition.llm_profile)
        if profile is None:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        locale = request.locale or conversation.definition.locale
        action = next(
            (a for a in conversation.actions if a.request.operation_id == request.action_id), None
        )
        message = request.text
        latest = message
        arrived = action is not None and isinstance(action.request.action, MoveAction)
        if action is not None:
            if action.origin == "conversation":
                if action.source is None:
                    raise KomaquillError(code="operation_conflict", retryable=False)
                message = action.source.remaining(request.text)
            latest = json.dumps(
                {
                    "speaker": "human_player",
                    **(
                        {}
                        if arrived
                        else {
                            "completed_action": {
                                "actor": "human_player",
                                **action.request.action.model_dump(mode="json", exclude_none=True),
                            }
                        }
                    ),
                    "message": message,
                },
                ensure_ascii=False,
            )
        if profile.provider == "fake":
            fixture = json.loads(
                files("komaquill").joinpath("resources/conversations/fixture.json").read_bytes()
            )
            if conversation.definition.presets and not conversation.definition.sample:
                raise KomaquillError(code="fake_input_mismatch", retryable=False)
            pair = fixture[locale]
            if conversation.definition.presets:
                samples = json.loads(
                    files("komaquill").joinpath("resources/library/samples.json").read_bytes()
                )
                sample_definition = conversation.definition
                matches = sample_definition.locale == locale and not sample_definition.instructions
                for key, model in (
                    ("user", PlayerInput),
                    ("character", CharacterInput),
                    ("scenario", StoryInput),
                ):
                    source = "player" if key == "user" else "story" if key == "scenario" else key
                    expected = model.model_validate(
                        samples["ja" if source == "player" else locale][source]
                    ).model_dump(mode="json", exclude={"character_id"})
                    try:
                        actual = getattr(sample_definition, key).model_dump()
                        if source == "story":
                            actual.update(
                                tone=sample_definition.tone,
                                topics=sample_definition.topics,
                                avoid=sample_definition.avoid,
                            )
                        matches = matches and actual == expected
                    except ValueError:
                        matches = False
                response = pair["response"]
            else:
                matches = not any(
                    (
                        conversation.definition.scenario.model_dump(exclude_defaults=True),
                        conversation.definition.character.model_dump(exclude_defaults=True),
                        conversation.definition.user.model_dump(exclude_defaults=True),
                        conversation.definition.instructions,
                        conversation.definition.initial_state.model_dump(exclude_defaults=True),
                    )
                )
                response = pair["unbound_response"]
            if not matches or conversation.state not in (
                conversation.definition.initial_state,
                Reply.model_validate(response).state_patch.apply(
                    conversation.definition.initial_state
                ),
            ):
                raise KomaquillError(code="fake_input_mismatch", retryable=False)
            if request.text not in pair["inputs"]:
                raise KomaquillError(code="fake_input_mismatch", retryable=False)
            try:
                from langchain_core.language_models.fake import FakeListLLM
                from langsmith import tracing_context
            except ImportError:
                raise KomaquillError(code="llm_dependencies_missing", retryable=False) from None
            self.execution.record(
                request.operation_id,
                0,
                {
                    "format_version": 1,
                    "provider": "fake",
                    "status": "fixture_response",
                    "locale": locale,
                    "input": request.text,
                    "response": response,
                },
            )
            fake = FakeListLLM(responses=[json.dumps(response, ensure_ascii=False)])
            with tracing_context(enabled=False):
                return Reply.model_validate_json(
                    fake.invoke(request.text, config={"callbacks": []})
                )
        if not request.allow_real_llm:
            raise KomaquillError(code="real_llm_not_allowed", retryable=False)
        language = "Japanese" if locale == "ja" else "English"
        reply_schema: type[NarrativeReply | ReactionReply]
        if arrived:
            reply_schema = ArrivalReply
        elif action is not None:
            reply_schema = ReactionReply
        else:
            reply_schema = NarrativeReply
        definition = conversation.definition
        story = definition.scenario.model_dump(exclude_defaults=True, exclude={"place", "goal"})
        if conversation.turns:
            story.pop("opening", None)
        scene = cast(dict[str, Any], scene_context(conversation))
        scene["current_state"] = {
            key: value for key, value in scene["current_state"].items() if value != ""
        }
        people = {}
        for role, state_role, person in (
            ("human_player", "player", definition.user),
            ("assistant_character", "character", definition.character),
        ):
            facts = scene["people"][state_role]
            people[role] = {
                **person.model_dump(
                    exclude_defaults=True,
                    exclude={
                        "age",
                        "gender",
                        "appearance",
                        "initial_clothing",
                        "initial_held_objects",
                        "visual_review_required",
                    },
                ),
                **{
                    key: value
                    for key, value in facts.items()
                    if value is not None and (value != "" or key == "clothing")
                },
            }
        context = {
            "story": story,
            "current_state": scene["current_state"],
            "meeting": request.meeting_state.answer().model_dump(mode="json", exclude_none=True)
            if request.meeting_state
            else {"status": "unknown"},
            "people": people,
            **({"pinned_notes": conversation.notes} if conversation.notes else {}),
            **(
                {
                    "style": {
                        key: getattr(definition, key)
                        for key in ("tone", "topics", "avoid")
                        if getattr(definition, key)
                    }
                }
                if any((definition.tone, definition.topics, definition.avoid))
                else {}
            ),
            "response_length": definition.response_length,
            "response_rules": {
                "held_objects": ObjectsChange.model_fields["value"].description,
                "action_narration": DialogueResult.model_fields["action_narration"].description,
                "text": DialogueResult.model_fields["text"].description,
            },
            "response_policy": definition.instructions,
            "response_language": language,
        }

        turn_contract = (
            "Both people are already here. Answer message from current_state.place. "
            "Describe the surroundings at this location if helpful. Leave action_narration "
            "empty; the app already shows the journey. If message is empty, acknowledge arrival."
            if arrived
            else "The completed_action is already applied in current_state. "
            "Respond to message from the current scene, starting with its question if any. "
            "If there is only an action, acknowledge it as complete. "
            "Return response.kind=companion."
            if action is not None
            else self.intent
        )
        reply_contract = (
            f"Write text, action_narration and scenery changes only in {language}. {turn_contract}"
        )

        def system(summary: ConversationMemory | None = None) -> str:
            values = {
                **context,
                **(
                    {
                        "historical_evidence": [
                            {"speaker": p.field, "text": p.quote} for p in summary.passages
                        ]
                    }
                    if summary
                    else {}
                ),
            }
            return (
                self.instructions
                + "\n"
                + json.dumps(values, ensure_ascii=False, separators=(",", ":"))
                + "\n"
                + reply_contract
            )

        field_language = "Japanese (日本語)" if locale == "ja" else "English"
        purpose = "move-response-" if request.action_origin == "conversation" else ""
        retained_response = self.execution.has_response(request.operation_id, purpose)
        messages: list[tuple[str, str]] = []
        active_memory = conversation.memory
        if not retained_response:
            try:
                from komaquill.adapters.llm.memory import history_messages

                messages, memory = history_messages(
                    self.execution,
                    conversation,
                    request,
                    system,
                    latest,
                    json.dumps(response_schema(reply_schema, field_language), ensure_ascii=False),
                    lambda through: self.read_history_hash(conversation.id, through),
                )
            except ImportError:
                raise KomaquillError(code="llm_dependencies_missing", retryable=False) from None
            if memory is not None:
                if self.save_memory is None:
                    raise KomaquillError(code="storage_failed", retryable=False)
                self.save_memory(conversation.id, memory)
            active_memory = memory or conversation.memory
            messages.append(("user", latest))
        names = [
            value
            for person in (conversation.definition.user, conversation.definition.character)
            for value in (person.name, person.address)
            if value
        ]

        def validate(reply: NarrativeReply | ReactionReply) -> None:
            if isinstance(reply, NarrativeReply) and reply.player_move is not None:
                try:
                    travel = SourceSpan.locate(message, reply.player_move.quote)
                    if reply.player_move.destination.strip() == conversation.state.place.strip():
                        raise ValueError("The characters are already at the destination.")
                    if (
                        isinstance(reply.response, DialogueResult)
                        and reply.response.meeting_change is not None
                    ):
                        meeting = SourceSpan.locate(message, reply.response.meeting_change.quote)
                        if travel.start < meeting.end and meeting.start < travel.end:
                            raise ValueError("A meeting decision cannot also be an immediate move.")
                except ValueError:
                    raise ResponseConstraintError("move_source", ("player_move", "quote")) from None
                return
            if isinstance(reply.response, ProposalResult):
                return
            value = reply.response
            if value.meeting_change is not None:
                try:
                    SourceSpan.locate(message, value.meeting_change.quote)
                    value.meeting_change.apply(
                        request.meeting_state, request.text, request.operation_id
                    )
                except ValueError:
                    raise ResponseConstraintError(
                        "meeting_source", ("response", "meeting_change")
                    ) from None
            if value.meeting_query == "only":
                return
            desired = resolve_scene(conversation, value)
            proposed = StatePatch.between(conversation.state, desired).apply(conversation.state)
            try:
                require_visible_reply(
                    conversation.state,
                    proposed,
                    value.text,
                    value.action_narration,
                    meeting_query=value.meeting_query == "mixed"
                    or value.meeting_change is not None,
                )
            except KomaquillError:
                raise ResponseConstraintError("empty_response", ("response", "text")) from None
            for field, content in (
                ("action_narration", value.action_narration),
                ("text", value.text),
                (
                    "scenery",
                    desired.scenery if desired.scenery != conversation.state.scenery else "",
                ),
            ):
                try:
                    check_language(content, locale, tuple(names))
                except ValueError:
                    raise ResponseLanguageError(field) from None
            if (
                proposed.player != conversation.state.player
                or proposed.place != conversation.state.place
            ):
                raise ValueError("Player changes require confirmation.")
            player_objects = {item.id for item in proposed.player.held_objects or []}
            for index, change in enumerate(value.changes):
                if isinstance(change, ObjectsChange):
                    for item_index, item in enumerate(change.value):
                        if item.id in player_objects:
                            raise ResponseConstraintError(
                                "object_ownership",
                                ("response", "changes", index, "value", item_index, "id"),
                            )

        if retained_response:
            generated = self.execution.replay(
                reply_schema,
                request.operation_id,
                conversation.definition.llm_profile,
                system(active_memory),
                validate,
                field_language,
                purpose=purpose,
            )
        else:
            generated = self.execution.invoke(
                reply_schema,
                request.operation_id,
                conversation.definition.llm_profile,
                request.allow_real_llm,
                system(active_memory),
                messages,
                validate=validate,
                correction=reply_contract,
                field_language=field_language,
                trim=False,
                purpose=purpose,
            )
        if isinstance(generated, NarrativeReply) and generated.player_move is not None:
            return InferredMove(
                action=MoveAction(destination=generated.player_move.destination),
                source=SourceSpan.locate(message, generated.player_move.quote),
            )
        value = generated.response
        if isinstance(value, ProposalResult):
            return Reply(text="", state_patch=StatePatch(), action_proposals=[value.action])
        if value.meeting_query == "only":
            return Reply(
                text="",
                state_patch=StatePatch(),
                meeting_change=value.meeting_change,
                meeting_query=True,
            )
        desired = resolve_scene(conversation, value)
        return Reply(
            narration=desired.scenery or desired.place,
            action_narration=value.action_narration,
            text=value.text,
            state_patch=StatePatch.between(conversation.state, desired),
            meeting_change=value.meeting_change,
            meeting_query=value.meeting_query == "mixed",
        )
