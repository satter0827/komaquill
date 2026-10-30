"""Share bounded, recorded requests for conversation, drafts and image translation."""

import hashlib
import json
import os
import re
import time
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from functools import cached_property
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

import keyring
from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.configuration import merge_settings
from komaquill.adapters.llm_configuration import LLMProfile
from komaquill.conversations.models import ConnectionProfile, Conversation, Reply, SendInput
from komaquill.domain.errors import KomaquillError
from komaquill.library.models import (
    CharacterInput,
    CreationRequest,
    PlayerInput,
    PreparedImage,
    StoryInput,
)

JAPANESE_TEXT = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
MIN_LANGUAGE_CHARACTERS = 12


class IllustratedReply(Reply):
    scene: str = Field(
        min_length=1,
        description=(
            "Required English visual description, including appearance and setting. "
            "Replace names with visible descriptions. Do not repeat the user's request."
        ),
    )


class StoryDraft(StoryInput):
    character_id: None = None


class ConversationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_timeout: float = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    repair_attempts: int = Field(ge=0, le=1)
    context_turns: int = Field(ge=1)
    max_input_characters: int = Field(gt=0)
    llm_profiles: dict[str, LLMProfile]


def load_settings(workspace: Path) -> ConversationSettings:
    defaults = tomllib.loads(
        files("komaquill")
        .joinpath("resources/conversations/defaults.toml")
        .read_text(encoding="utf-8")
    )
    configured = tomllib.loads((workspace / "komaquill.toml").read_text(encoding="utf-8"))
    data = merge_settings(defaults, configured.get("conversation", {}))
    if not isinstance(data["llm_profiles"], dict):
        raise KomaquillError(code="invalid_configuration", retryable=False)
    data["llm_profiles"] = merge_settings(data["llm_profiles"], configured.get("llm_profiles", {}))
    return ConversationSettings.model_validate(data)


def check_language(text: str, locale: str, names: tuple[str, ...] = ()) -> None:
    for name in sorted(names, key=len, reverse=True):
        text = text.replace(name, "")
    japanese = bool(JAPANESE_TEXT.search(text))
    if (locale == "en" and japanese) or (
        locale == "ja" and len(text.strip()) >= MIN_LANGUAGE_CHARACTERS and not japanese
    ):
        raise ValueError("response_language")


class StructuredResponder:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.instructions = (
            files("komaquill")
            .joinpath("resources/conversations/instructions.txt")
            .read_text(encoding="utf-8")
        )

    @cached_property
    def settings(self) -> ConversationSettings:
        try:
            return load_settings(self.workspace)
        except (OSError, ValueError):
            raise KomaquillError(code="invalid_configuration", retryable=False) from None

    def profiles(self) -> list[ConnectionProfile]:
        return [
            ConnectionProfile(id=name, provider=p.provider, model=p.model)
            for name, p in self.settings.llm_profiles.items()
        ]

    def record(
        self, operation: UUID, attempt: int, payload: dict[str, Any], purpose: str = ""
    ) -> None:
        atomic_json(
            self.workspace
            / ".artifacts/data/conversation-calls"
            / str(operation)
            / f"{purpose}{attempt}.json",
            payload,
        )

    def _model(self, profile: LLMProfile) -> Any:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise KomaquillError(code="llm_dependencies_missing") from None
        key = os.environ.get(profile.key_env) if profile.key_env else None
        if key is None and profile.credential_ref:
            key = keyring.get_password("komaquill.llm", profile.credential_ref)
        if key is None and urlsplit(profile.base_url or "").hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            key = "local"
        if not key:
            raise KomaquillError(code="authentication_failed", retryable=False)
        return ChatOpenAI(
            model=profile.model,
            base_url=profile.base_url,
            api_key=SecretStr(key),
            timeout=self.settings.request_timeout,
            max_completion_tokens=self.settings.max_output_tokens,
            max_retries=0,
            temperature=profile.temperature,
            reasoning_effort=profile.reasoning_effort,
            use_responses_api=False,
            stream_usage=False,
            cache=False,
        )

    def invoke[T: BaseModel](
        self,
        schema: type[T],
        operation: UUID,
        profile_id: str,
        permitted: bool,
        system: str,
        messages: list[tuple[str, str]],
        *,
        purpose: str = "",
        validate: Callable[[T], None] | None = None,
        correction: str = "Return the complete result in the required language.",
    ) -> T:
        profile = self.settings.llm_profiles.get(profile_id)
        if profile is None:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        if profile.provider == "fake":
            raise KomaquillError(code="fake_input_mismatch", retryable=False)
        if not permitted:
            raise KomaquillError(code="real_llm_not_allowed", retryable=False)
        try:
            from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
            from langsmith import tracing_context
        except ImportError:
            raise KomaquillError(code="llm_dependencies_missing") from None
        context: list[BaseMessage] = [SystemMessage(content=system)]
        context.extend(
            AIMessage(content=text) if role == "assistant" else HumanMessage(content=text)
            for role, text in messages
        )
        schema_chars = len(json.dumps(schema.model_json_schema(), ensure_ascii=False))
        while len(context) > 2 and (
            sum(len(str(m.content)) for m in context) + schema_chars
            > self.settings.max_input_characters
        ):
            del context[1:3]
        bound = self._model(profile).bind(
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "conversation_reply"
                    if issubclass(schema, Reply)
                    else "creation_result",
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            }
        )
        lock_path = self.workspace / ".artifacts/state/.llm.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(str(lock_path), timeout=0):
                for attempt in range(self.settings.repair_attempts + 1):
                    if (
                        sum(len(str(m.content)) for m in context) + schema_chars
                        > self.settings.max_input_characters
                    ):
                        raise KomaquillError(code="context_limit_exceeded", retryable=False)
                    record = {
                        "format_version": 1,
                        "status": "started",
                        "model": profile.model,
                        "profile": profile_id,
                        "started_at": datetime.now(UTC).isoformat(),
                        "schema": schema.model_json_schema(),
                        "instructions_sha256": hashlib.sha256(system.encode()).hexdigest(),
                        "messages": [{"role": m.type, "content": m.content} for m in context],
                        "max_output_tokens": self.settings.max_output_tokens,
                        "temperature": profile.temperature,
                        "reasoning_effort": profile.reasoning_effort,
                        "usage": None,
                    }
                    self.record(operation, attempt, record, purpose)
                    started = time.monotonic()
                    try:
                        with tracing_context(enabled=False):
                            response = bound.invoke(context, config={"callbacks": []})
                    except Exception as error:
                        status = getattr(error, "status_code", None)
                        code = (
                            "llm_request_invalid"
                            if status in {400, 422}
                            else (
                                "authentication_failed"
                                if status in {401, 403}
                                else "llm_unavailable"
                            )
                        )
                        self.record(
                            operation,
                            attempt,
                            {
                                **record,
                                "status": "failed",
                                "error": code,
                                "exception_type": type(error).__name__,
                                "http_status": status if isinstance(status, int) else None,
                                "elapsed_seconds": time.monotonic() - started,
                            },
                            purpose,
                        )
                        raise KomaquillError(code=code, retryable=False) from None
                    actual = response.response_metadata.get(
                        "model_name"
                    ) or response.response_metadata.get("model")
                    self.record(
                        operation,
                        attempt,
                        {
                            **record,
                            "status": "response_received",
                            "response_model": actual,
                            "finish_reason": response.response_metadata.get("finish_reason"),
                            "response": response.content,
                            "usage": response.usage_metadata,
                            "elapsed_seconds": time.monotonic() - started,
                        },
                        purpose,
                    )
                    if profile.require_model_match and actual != profile.model:
                        raise KomaquillError(code="identity_mismatch", retryable=False)
                    if response.additional_kwargs.get("refusal") or not isinstance(
                        response.content, str
                    ):
                        raise KomaquillError(code="invalid_response", retryable=False)
                    try:
                        value = schema.model_validate_json(response.content)
                        if validate:
                            validate(value)
                        return value
                    except (ValidationError, ValueError):
                        context.extend(
                            [
                                AIMessage(content=response.content),
                                HumanMessage(
                                    content=(
                                        "Correct the previous response. Return valid JSON matching "
                                        "the supplied schema. Follow the required output language "
                                        "exactly, including after a language switch. "
                                        "Preserve saved proper names in visible text. "
                                        "For images replace names with English "
                                        "visual descriptions. "
                                        "Do not change facts. " + correction
                                    )
                                ),
                            ]
                        )
        except Timeout:
            raise KomaquillError(code="operation_busy", retryable=True) from None
        raise KomaquillError(code="invalid_response", retryable=False)

    def reply(self, conversation: Conversation, request: SendInput) -> Reply:
        profile = self.settings.llm_profiles.get(conversation.definition.llm_profile)
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
                    expected = model.model_validate(samples[locale][source]).model_dump(
                        mode="json", exclude={"character_id"}
                    )
                    try:
                        matches = (
                            matches and json.loads(getattr(sample_definition, key)) == expected
                        )
                    except ValueError:
                        matches = False
                response = pair["response"]
            else:
                matches = not any(
                    (
                        conversation.definition.scenario,
                        conversation.definition.character,
                        conversation.definition.user,
                        conversation.definition.instructions,
                        conversation.definition.initial_state,
                    )
                )
                response = pair["unbound_response"]
            if not matches or conversation.state not in (
                conversation.definition.initial_state,
                response["state"],
            ):
                raise KomaquillError(code="fake_input_mismatch", retryable=False)
            if request.text not in pair["inputs"]:
                raise KomaquillError(code="fake_input_mismatch", retryable=False)
            try:
                from langchain_core.language_models.fake import FakeListLLM
                from langsmith import tracing_context
            except ImportError:
                raise KomaquillError(code="llm_dependencies_missing", retryable=False) from None
            self.record(
                request.operation_id,
                0,
                {"provider": "fake", "locale": locale, "input": request.text, "response": response},
            )
            fake = FakeListLLM(responses=[json.dumps(response, ensure_ascii=False)])
            with tracing_context(enabled=False):
                return Reply.model_validate_json(
                    fake.invoke(request.text, config={"callbacks": []})
                )
        language = "Japanese" if locale == "ja" else "English"
        definition = conversation.definition.model_dump(mode="json", exclude={"presets"})
        definition["locale"] = locale
        definition["images"] = (
            request.images if request.images is not None else conversation.definition.images
        )
        definition["response_length"] = (
            request.response_length or conversation.definition.response_length
        )
        system = (
            self.instructions
            + "\n"
            + json.dumps(
                {
                    "definition": definition,
                    "state": conversation.state,
                    "response_language": language,
                },
                ensure_ascii=False,
            )
            + f"\nThe visible text must be {language}. Image scene descriptions must be English."
        )
        messages: list[tuple[str, str]] = []
        for turn in conversation.turns[-self.settings.context_turns :]:
            messages.extend([("user", turn.user), ("assistant", turn.assistant)])
        scene_contract = (
            "The scene field must be a nonempty English visual description, never null. "
            if request.images is True
            else "The scene field must be null or an English visual description. "
        )
        language_contract = (
            f"The JSON text field must be written in {language}. "
            + scene_contract
            + "Replace character names in scene with their visible appearance. "
            "Do not write Japanese in scene or repeat the user's request. "
            "Image descriptions belong in scene, not in state."
        )
        messages.append(("user", request.text + "\n\n" + language_contract))
        names: list[str] = []
        for text in [conversation.definition.user, conversation.definition.character]:
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if isinstance(value, dict):
                names.extend(str(value[key]) for key in ("name", "address") if value.get(key))

        def validate(reply: Reply) -> None:
            check_language(reply.text, locale, tuple(names))
            if reply.scene:
                check_language(reply.scene, "en")

        return self.invoke(
            IllustratedReply if request.images is True else Reply,
            request.operation_id,
            conversation.definition.llm_profile,
            request.allow_real_llm,
            system,
            messages,
            validate=validate,
            correction=language_contract,
        )

    def draft(self, request: CreationRequest) -> CharacterInput | StoryInput:
        language = "Japanese" if request.locale == "ja" else "English"
        schema = CharacterInput if request.kind == "character_draft" else StoryDraft

        def validate(value: BaseModel) -> None:
            names = tuple(
                str(name) for name in value.model_dump(include={"name", "address"}).values() if name
            )
            check_language(value.model_dump_json(), request.locale, names)

        result = self.invoke(
            schema,
            request.operation_id,
            request.llm_profile,
            request.allow_real_llm,
            f"Create a concise {request.kind} from the user's directions. Write visible fields in "
            f"{language}. Preserve requested facts. Do not add identifiers or hidden reasoning.",
            [("user", request.prompt + f"\n\nWrite the draft in {language}.")],
            purpose="draft-",
            validate=validate,
            correction=f"Write the draft fields in {language}.",
        )
        return cast(CharacterInput | StoryInput, result)

    def prepare_image(self, text: str, operation: UUID, profile: str, allow_real_llm: bool) -> str:
        if not JAPANESE_TEXT.search(text):
            return text
        value = self.invoke(
            PreparedImage,
            operation,
            profile,
            allow_real_llm,
            "Translate the visual description into English for image generation. "
            "Preserve appearance, actions, objects and setting. "
            "Replace names with visible descriptions. Return JSON with "
            "English text, without quality tags or hidden reasoning.",
            [("user", text + "\n\nWrite the image description in English only.")],
            purpose="image-",
            validate=lambda result: check_language(result.text, "en"),
            correction="Translate the entire text field to English. Do not retain Japanese names.",
        )
        return value.text
