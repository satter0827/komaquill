"""Compose conversation context and validate visible replies."""

import json
from collections.abc import Callable
from importlib.resources import files
from typing import Self
from uuid import UUID

from filelock import Timeout
from pydantic import Field, model_validator

from komaquill.adapters.llm.execution import StructuredExecution, response_schema
from komaquill.adapters.llm.language import ResponseLanguageError, check_language
from komaquill.conversations.context import scene_context
from komaquill.conversations.models import ConnectionProfile, Conversation, Reply, SendInput
from komaquill.domain.errors import KomaquillError
from komaquill.domain.roleplay import (
    ConversationMemory,
    ConversationState,
    PersonState,
    Record,
    StatePatch,
)
from komaquill.library.models import (
    CharacterInput,
    PlayerInput,
    StoryInput,
)


class GeneratedPersonState(PersonState):
    """Require every current property in the model response, including unknown values."""

    appearance: str | None
    clothing: str | None
    expression: str | None
    pose: str | None
    held_objects: list[str] | None = Field(
        description=PersonState.model_fields["held_objects"].description
    )


class GeneratedState(ConversationState):
    place: str
    time: str
    goal: str
    relationship: str
    player: GeneratedPersonState = Field(
        description="Only human_player. Update for explicit first-person player actions."
    )
    character: GeneratedPersonState = Field(
        description="You, assistant_character. Perform second-person requests and imperatives here."
    )

    @model_validator(mode="after")
    def canonical_limits(self) -> Self:
        ConversationState.model_validate(self.model_dump())
        return self


class NarrativeReply(Record):
    current_state: GeneratedState = Field(
        description=(
            "Complete state AFTER the latest player request and this turn's actions. "
            "Carry forward unchanged facts. Move place when requested. "
            "Null person fields mean unknown, not removal. "
            "Use empty clothing only when the person explicitly removes it."
        )
    )
    narration: str = Field(
        min_length=1,
        max_length=20000,
        description=(
            "Third-person surroundings and actions of assistant_character, "
            "without quoted dialogue. "
            "Do not invent human_player speech or actions."
        ),
    )
    text: str = Field(
        min_length=1, max_length=20000, description=Reply.model_fields["text"].description
    )


class ConversationResponder:
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
                return self._reply(conversation, request)
        except Timeout:
            raise KomaquillError(code="operation_busy", retryable=True) from None

    def _reply(self, conversation: Conversation, request: SendInput) -> Reply:
        profile = self.execution.settings.llm_profiles.get(conversation.definition.llm_profile)
        if profile is None:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        locale = request.locale or conversation.definition.locale
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
            "assistant_character": definition.character.model_dump(exclude_defaults=True),
            "human_player": definition.user.model_dump(exclude_defaults=True),
            "story": story,
            **scene_context(conversation),
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
                + f"\nThe visible text and narration must be {language}."
            )

        language_contract = (
            f"The JSON text and narration fields must be written in {language}. "
            "Return the complete updated current_state before narration and dialogue."
        )
        field_language = "Japanese (日本語)" if locale == "ja" else "English"
        try:
            from komaquill.adapters.llm.memory import history_messages

            messages, memory = history_messages(
                self.execution,
                conversation,
                request,
                system,
                request.text + "\n\n" + language_contract,
                json.dumps(response_schema(NarrativeReply, field_language), ensure_ascii=False),
            )
        except ImportError:
            raise KomaquillError(code="llm_dependencies_missing", retryable=False) from None
        if memory is not None:
            if self.save_memory is None:
                raise KomaquillError(code="storage_failed", retryable=False)
            self.save_memory(conversation.id, memory)
        active_memory = memory or conversation.memory
        messages.append(("user", request.text + "\n\n" + language_contract))
        names = [
            value
            for person in (conversation.definition.user, conversation.definition.character)
            for value in (person.name, person.address)
            if value
        ]

        def validate(reply: NarrativeReply) -> None:
            for field in ("text", "narration"):
                try:
                    check_language(getattr(reply, field), locale, tuple(names))
                except ValueError:
                    raise ResponseLanguageError(field) from None

        generated = self.execution.invoke(
            NarrativeReply,
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
        desired = generated.current_state
        return Reply(
            narration=generated.narration,
            text=generated.text,
            state_patch=StatePatch.between(conversation.state, desired),
        )
