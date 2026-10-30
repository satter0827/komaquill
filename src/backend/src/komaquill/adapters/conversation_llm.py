"""Call one structured-response model with explicit permission and bounded repair."""

import hashlib
import json
import os
import tomllib
from functools import cached_property
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import keyring
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.configuration import merge_settings
from komaquill.adapters.llm_configuration import LLMProfile
from komaquill.conversations.models import ConnectionProfile, Conversation, Reply, SendInput
from komaquill.domain.errors import KomaquillError


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
    profiles = data["llm_profiles"]
    if not isinstance(profiles, dict):
        raise KomaquillError(code="invalid_configuration", retryable=False)
    data["llm_profiles"] = merge_settings(profiles, configured.get("llm_profiles", {}))
    return ConversationSettings.model_validate(data)


class StructuredResponder:
    def profiles(self) -> list[ConnectionProfile]:
        return [
            ConnectionProfile(id=name, provider=value.provider, model=value.model)
            for name, value in self.settings.llm_profiles.items()
        ]

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

    def record(self, operation: UUID, attempt: int, payload: dict[str, Any]) -> None:
        path = (
            self.workspace
            / ".artifacts/data/conversation-calls"
            / str(operation)
            / f"{attempt}.json"
        )
        atomic_json(path, payload)

    def reply(self, conversation: Conversation, request: SendInput) -> Reply:
        profile = self.settings.llm_profiles.get(conversation.definition.llm_profile)
        if profile is None:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        if profile.provider != "fake" and not request.allow_real_llm:
            raise KomaquillError(code="real_llm_not_allowed", retryable=False)
        try:
            from langchain_core.language_models.fake import FakeListLLM
            from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
            from langsmith import tracing_context
        except ImportError:
            raise KomaquillError(code="llm_dependencies_missing") from None
        if profile.provider == "fake":
            fixture = json.loads(
                files("komaquill").joinpath("resources/conversations/fixture.json").read_bytes()
            )
            if request.text != fixture["input"]:
                raise KomaquillError(code="fake_input_mismatch", retryable=False)
            with tracing_context(enabled=False):
                content = FakeListLLM(responses=[json.dumps(fixture["response"])]).invoke(
                    request.text, config={"callbacks": []}
                )
            self.record(
                request.operation_id,
                0,
                {"provider": "fake", "input": request.text, "response": content},
            )
            return Reply.model_validate_json(content)
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
        model = ChatOpenAI(
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
        bound = model.bind(
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "conversation_reply",
                    "strict": True,
                    "schema": Reply.model_json_schema(),
                },
            }
        )
        messages: list[BaseMessage] = [
            SystemMessage(content=self.instructions),
            SystemMessage(
                content=json.dumps(
                    {
                        "definition": conversation.definition.model_dump(mode="json"),
                        "state": conversation.state,
                    },
                    ensure_ascii=False,
                )
            ),
        ]
        for turn in conversation.turns[-self.settings.context_turns :]:
            messages.extend([HumanMessage(content=turn.user), AIMessage(content=turn.assistant)])
        messages.append(HumanMessage(content=request.text))
        history_pairs = max(0, (len(messages) - 3) // 2)
        schema_characters = len(json.dumps(Reply.model_json_schema(), ensure_ascii=False))
        while (
            history_pairs
            and sum(len(str(item.content)) for item in messages) + schema_characters
            > self.settings.max_input_characters
        ):
            del messages[2:4]
            history_pairs -= 1
        for attempt in range(self.settings.repair_attempts + 1):
            if (
                sum(len(str(item.content)) for item in messages) + schema_characters
                > self.settings.max_input_characters
            ):
                raise KomaquillError(code="context_limit_exceeded", retryable=False)
            request_record = {
                "format_version": 1,
                "status": "started",
                "model": profile.model,
                "instructions_sha256": hashlib.sha256(self.instructions.encode()).hexdigest(),
                "messages": [
                    {"role": message.type, "content": message.content} for message in messages
                ],
                "max_output_tokens": self.settings.max_output_tokens,
                "temperature": profile.temperature,
                "reasoning_effort": profile.reasoning_effort,
                "usage": None,
            }
            self.record(request.operation_id, attempt, request_record)
            try:
                with tracing_context(enabled=False):
                    response = bound.invoke(messages, config={"callbacks": []})
            except Exception:
                self.record(
                    request.operation_id,
                    attempt,
                    {**request_record, "status": "failed", "error": "llm_unavailable"},
                )
                raise KomaquillError(code="llm_unavailable", retryable=False) from None
            actual = response.response_metadata.get("model_name") or response.response_metadata.get(
                "model"
            )
            self.record(
                request.operation_id,
                attempt,
                {
                    **request_record,
                    "status": "response_received",
                    "response_model": actual,
                    "finish_reason": response.response_metadata.get("finish_reason"),
                    "response": response.content,
                    "usage": response.usage_metadata,
                },
            )
            if profile.require_model_match and actual != profile.model:
                raise KomaquillError(code="identity_mismatch", retryable=False)
            if response.additional_kwargs.get("refusal") or not isinstance(response.content, str):
                raise KomaquillError(code="invalid_response", retryable=False)
            try:
                return Reply.model_validate_json(response.content)
            except ValidationError:
                messages.extend(
                    [
                        AIMessage(content=response.content),
                        HumanMessage(
                            content=(
                                "Return valid JSON matching the supplied schema, with nonempty "
                                "text, string-valued state and a scene or null."
                            )
                        ),
                    ]
                )
        raise KomaquillError(code="invalid_response", retryable=False)
