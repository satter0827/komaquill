"""Compose conversation context and validate visible replies."""

import json
from collections.abc import Callable
from functools import cached_property
from importlib.resources import files
from typing import Annotated, Any, Literal
from uuid import UUID

from filelock import Timeout
from pydantic import Field

from komaquill.adapters.llm.constraints import ResponseConstraintError
from komaquill.adapters.llm.execution import StructuredExecution, response_schema
from komaquill.adapters.llm.language import ResponseLanguageError, check_language
from komaquill.conversations.actions import IntentionAction, MoveAction, PrepareAction, WaitAction
from komaquill.conversations.context import scene_context
from komaquill.conversations.models import ConnectionProfile, Conversation, Reply, SendInput
from komaquill.conversations.transitions import published_reply
from komaquill.domain.errors import KomaquillError
from komaquill.domain.roleplay import (
    ConversationMemory,
    ConversationState,
    PersonState,
    PhysicalObjects,
    Record,
    StatePatch,
)
from komaquill.library.models import (
    CharacterInput,
    PlayerInput,
    StoryInput,
)


class GeneratedPersonState(Record):
    """Require every current property in the model response, including unknown values."""

    clothing: str | None = Field(max_length=3000)
    expression: str | None = Field(max_length=1000)
    pose: str | None = Field(max_length=1000)
    held_objects: PhysicalObjects | None = Field(
        description=PersonState.model_fields["held_objects"].description
    )


class GeneratedState(Record):
    relationship: str = Field(max_length=2000)
    companion_movement: Literal["stay", "join_player"] = Field(
        description=(
            "stay keeps the companion's location; "
            "join_player moves the companion to the player's current place."
        )
    )
    scenery: str = Field(
        max_length=4000,
        description="Visible environment, furniture, weather and lighting at the player's place. "
        "Do not include people, dialogue or unconfirmed player actions.",
    )
    character: GeneratedPersonState = Field(
        description="You, assistant_character. Perform second-person requests and imperatives here."
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


def resolve_scene(conversation: Conversation, generated: GeneratedState) -> ConversationState:
    return conversation.state.model_copy(
        update={
            "relationship": generated.relationship,
            "scenery": generated.scenery,
            "character_place": conversation.state.place
            if generated.companion_movement == "join_player"
            else conversation.state.character_place,
            "character": conversation.state.character.model_copy(
                update={
                    key: getattr(generated.character, key)
                    for key in type(generated.character).model_fields
                    if getattr(generated.character, key) is not None
                }
            ),
        }
    )


class NarrativeReply(Record):
    requested_player_action: GeneratedAction | None = Field(
        description=(
            "Extract one action the human player explicitly requests for themselves in the latest "
            "message. Return null for questions, dialogue and companion requests. "
            "First-person clothing changes require kind=prepare with clothing; movement requires "
            "kind=move. Do not complete these actions in scenery or classify them as intention. "
            "Never suggest possible future actions."
        ),
    )
    current_state: GeneratedState = Field(
        description=(
            "Permitted scene and companion changes only. Never change player state. "
            "Carry forward unchanged facts. "
            "Null person fields mean unknown, not removal. "
            "Use empty clothing only when the person explicitly removes it."
        )
    )
    action_narration: str = Field(
        max_length=6000,
        description="Visible actions by the companion, consistent with current_state. "
        "Never invent player actions or feelings.",
    )
    text: str = Field(max_length=20000, description=Reply.model_fields["text"].description)


class ReactionReply(NarrativeReply):
    requested_player_action: None


class ConversationResponder:
    @cached_property
    def graph(self) -> Any:
        from komaquill.adapters.llm.workflow import compile_conversation

        return compile_conversation(self.execution.checkpoints)

    def __init__(
        self,
        execution: StructuredExecution,
        save_memory: Callable[[UUID, ConversationMemory], None],
    ) -> None:
        self.execution = execution
        self.save_memory = save_memory
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

    def reply(self, conversation: Conversation, request: SendInput) -> Reply:
        try:
            with self.execution.lock:
                from komaquill.adapters.llm.workflow import run_conversation

                return run_conversation(self.graph, conversation, request, self._reply)
        except Timeout:
            raise KomaquillError(code="operation_busy", retryable=True) from None
        except ImportError:
            raise KomaquillError(code="llm_dependencies_missing", retryable=False) from None

    def _reply(self, conversation: Conversation, request: SendInput) -> Reply:
        profile = self.execution.settings.llm_profiles.get(conversation.definition.llm_profile)
        if profile is None:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        locale = request.locale or conversation.definition.locale
        action = next(
            (a for a in conversation.actions if a.request.operation_id == request.action_id), None
        )
        latest = (
            "React to this confirmed player action without changing its result: "
            + action.request.action.model_dump_json()
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
            **scene_context(conversation),
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

        def system(summary: str | None = None) -> str:
            values = {**context, **({"historical_summary": summary} if summary else {})}
            return (
                self.instructions
                + "\n"
                + json.dumps(values, ensure_ascii=False)
                + f"\nThe visible text and scenery must be {language}."
            )

        language_contract = (
            f"The JSON text and current_state.scenery must be written in {language}. "
            "Return the permitted companion and scenery fields before dialogue."
        )
        reply_schema = ReactionReply if request.action_id is not None else NarrativeReply
        field_language = "Japanese (日本語)" if locale == "ja" else "English"
        try:
            from komaquill.adapters.llm.memory import history_messages

            messages, memory = history_messages(
                self.execution,
                conversation,
                request,
                system,
                latest + "\n\n" + language_contract,
                json.dumps(response_schema(reply_schema, field_language), ensure_ascii=False),
            )
        except ImportError:
            raise KomaquillError(code="llm_dependencies_missing", retryable=False) from None
        if memory is not None:
            if self.save_memory is None:
                raise KomaquillError(code="storage_failed", retryable=False)
            self.save_memory(conversation.id, memory)
        active_memory = memory or conversation.memory
        messages.append(("user", latest + "\n\n" + language_contract))
        names = [
            value
            for person in (conversation.definition.user, conversation.definition.character)
            for value in (person.name, person.address)
            if value
        ]

        def validate(reply: NarrativeReply) -> None:
            if reply.requested_player_action is not None:
                return
            desired = resolve_scene(conversation, reply.current_state)
            proposed = StatePatch.between(conversation.state, desired).apply(conversation.state)
            for field, value in (
                ("action_narration", reply.action_narration),
                ("text", reply.text if proposed.character_place == proposed.place else ""),
                ("scenery", reply.current_state.scenery),
            ):
                try:
                    check_language(value, locale, tuple(names))
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

        generated = self.execution.invoke(
            reply_schema,
            request.operation_id,
            conversation.definition.llm_profile,
            request.allow_real_llm,
            system(active_memory.text if active_memory else None),
            messages,
            validate=validate,
            correction=language_contract,
            field_language=field_language,
            trim=False,
        )
        desired = resolve_scene(conversation, generated.current_state)
        return published_reply(
            conversation,
            Reply(
                narration=desired.scenery,
                action_narration=generated.action_narration,
                text=generated.text,
                state_patch=StatePatch.between(conversation.state, desired),
                action_proposals=[generated.requested_player_action]
                if generated.requested_player_action is not None
                else [],
            ),
        )
