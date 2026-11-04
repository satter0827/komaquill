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
from komaquill.conversations.actions import IntentionAction, MoveAction, PrepareAction, WaitAction
from komaquill.conversations.context import scene_context
from komaquill.conversations.models import ConnectionProfile, Conversation, Reply, TurnRequest
from komaquill.conversations.transitions import published_reply
from komaquill.domain.errors import KomaquillError
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
from komaquill.library.models import (
    CharacterInput,
    PlayerInput,
    StoryInput,
)


class ProposedMove(MoveAction):
    kind: Literal["move"]


class ProposedWait(WaitAction):
    kind: Literal["wait"]


class ProposedPreparation(PrepareAction):
    """A physical self-action: changing clothes, pose, expression or held objects."""

    kind: Literal["prepare"]


class ProposedIntention(IntentionAction):
    """An ongoing personal goal, never a physical action or a request to the companion."""

    kind: Literal["intention"]


GeneratedAction = Annotated[
    ProposedMove | ProposedWait | ProposedPreparation | ProposedIntention,
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
    value: str = Field(max_length=MAX_CLOTHING_LENGTH)


class ExpressionPoseChange(Record):
    field: Literal["expression", "pose"]
    value: str = Field(max_length=MAX_EXPRESSION_POSE_LENGTH)


class MovementChange(Record):
    field: Literal["movement"]
    value: Literal["stay", "join_player"]


class ObjectsChange(Record):
    field: Literal["held_objects"]
    value: PhysicalObjects


SceneChange = Annotated[
    RelationshipChange
    | SceneryChange
    | ClothingChange
    | ExpressionPoseChange
    | MovementChange
    | ObjectsChange,
    Field(discriminator="field"),
]


class DialogueResult(Record):
    kind: Literal["companion"]
    changes: list[SceneChange] = Field(max_length=7)
    action_narration: str = Field(max_length=6000)
    text: str = Field(max_length=20000, description=Reply.model_fields["text"].description)

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


class ReactionReply(NarrativeReply):
    response: DialogueResult


def resolve_scene(conversation: Conversation, generated: DialogueResult) -> ConversationState:
    changes: dict[str, Any] = {}
    for change in generated.changes:
        if change.field == "movement":
            if change.value == "join_player":
                changes["character_place"] = conversation.state.place
        elif change.field in {"relationship", "scenery"}:
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
        self.instructions = (
            files("komaquill")
            .joinpath("resources/conversations/instructions.txt")
            .read_text(encoding="utf-8")
        )

    def profiles(self) -> list[ConnectionProfile]:
        return [
            ConnectionProfile(id=name, provider=p.provider, model=p.model)
            for name, p in self.execution.settings.llm_profiles.items()
        ]

    def reply(self, conversation: Conversation, request: TurnRequest) -> Reply:
        try:
            with self.execution.lock:
                from komaquill.adapters.llm.workflow import run_conversation

                return run_conversation(self.graph, conversation, request, self._reply)
        except Timeout:
            raise KomaquillError(code="operation_busy", retryable=True) from None
        except ImportError:
            raise KomaquillError(code="llm_dependencies_missing", retryable=False) from None

    def _reply(self, conversation: Conversation, request: TurnRequest) -> Reply:
        profile = self.execution.settings.llm_profiles.get(conversation.definition.llm_profile)
        if profile is None:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        locale = request.locale or conversation.definition.locale
        action = next(
            (a for a in conversation.actions if a.request.operation_id == request.action_id), None
        )
        latest = (
            "The human player has completed this action. React as the companion; "
            "keep the companion's own clothing and possessions: "
            + action.request.action.model_dump_json()
            + "\nThe player also says: "
            + request.text
            if action is not None
            else request.text
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
                        matches = (
                            matches and getattr(sample_definition, key).model_dump() == expected
                        )
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
                {"provider": "fake", "locale": locale, "input": request.text, "response": response},
            )
            fake = FakeListLLM(responses=[json.dumps(response, ensure_ascii=False)])
            with tracing_context(enabled=False):
                return Reply.model_validate_json(
                    fake.invoke(request.text, config={"callbacks": []})
                )
        if not request.allow_real_llm:
            raise KomaquillError(code="real_llm_not_allowed", retryable=False)
        language = "Japanese" if locale == "ja" else "English"
        definition = conversation.definition
        story = definition.scenario.model_dump(exclude_defaults=True, exclude={"place", "goal"})
        if conversation.turns:
            story.pop("opening", None)
        scene = cast(dict[str, Any], scene_context(conversation))
        scene["current_state"] = {
            key: value for key, value in scene["current_state"].items() if value != ""
        }
        scene["people"] = {
            role: {
                key: value
                for key, value in person.items()
                if value is not None and (value != "" or key == "clothing")
            }
            for role, person in scene["people"].items()
        }
        context = {
            "assistant_character": definition.character.model_dump(
                exclude_defaults=True,
                exclude={
                    "appearance",
                    "age",
                    "gender",
                    "initial_clothing",
                    "initial_held_objects",
                    "visual_review_required",
                },
            ),
            "human_player": definition.user.model_dump(
                exclude_defaults=True,
                exclude={
                    "appearance",
                    "age",
                    "gender",
                    "initial_clothing",
                    "initial_held_objects",
                    "visual_review_required",
                },
            ),
            "story": story,
            **scene,
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
            "response_policy": definition.instructions,
            "response_language": language,
        }

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
                + f"\nWrite text, action_narration and changes to scenery only in {language}."
            )

        language_contract = (
            "Write dialogue, action_narration and scenery changes in " + language + ". "
            "Use kind=player for a future player action; use kind=companion otherwise."
        )
        reply_schema = ReactionReply if request.action_id is not None else NarrativeReply
        field_language = "Japanese (日本語)" if locale == "ja" else "English"
        retained_response = self.execution.has_response(request.operation_id)
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

        def validate(reply: NarrativeReply) -> None:
            if isinstance(reply.response, ProposalResult):
                return
            value = reply.response
            desired = resolve_scene(conversation, value)
            proposed = StatePatch.between(conversation.state, desired).apply(conversation.state)
            if (
                action is None
                and proposed.character_place == proposed.place
                and not value.text.strip()
                and not value.action_narration.strip()
                and proposed == conversation.state
            ):
                raise ResponseConstraintError("empty_response")
            for field, content in (
                ("action_narration", value.action_narration),
                ("text", value.text if proposed.character_place == proposed.place else ""),
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
            if {item.id for item in proposed.character.held_objects or []}.intersection(
                item.id for item in proposed.player.held_objects or []
            ):
                raise ResponseConstraintError("object_ownership")

        if retained_response:
            generated = self.execution.replay(
                reply_schema,
                request.operation_id,
                conversation.definition.llm_profile,
                system(active_memory),
                validate,
                field_language,
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
                correction=language_contract,
                field_language=field_language,
                trim=False,
            )
        value = generated.response
        if isinstance(value, ProposalResult):
            return Reply(text="", state_patch=StatePatch(), action_proposals=[value.action])
        desired = resolve_scene(conversation, value)
        return published_reply(
            conversation,
            Reply(
                narration=desired.scenery,
                action_narration=value.action_narration,
                text=value.text,
                state_patch=StatePatch.between(conversation.state, desired),
            ),
        )
