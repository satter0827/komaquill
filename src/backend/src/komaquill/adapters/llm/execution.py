"""Execute bounded structured requests and preserve every call record."""

import hashlib
import json
import os
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import keyring
from filelock import FileLock, Timeout
from pydantic import BaseModel, SecretStr

from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.configuration import WorkspaceConfiguration
from komaquill.adapters.llm.constraints import ResponseConstraintError
from komaquill.adapters.llm.language import ResponseLanguageError
from komaquill.adapters.llm_configuration import ConversationSettings, LLMProfile
from komaquill.domain.errors import KomaquillError


def response_schema(schema: type[BaseModel], field_language: str | None) -> dict[str, Any]:
    """Use the same wire schema for context budgeting and the SDK request."""
    result = deepcopy(schema.model_json_schema())
    if field_language:
        for field in ("narration", "action_narration", "text"):
            if field not in result["properties"]:
                continue
            meaning = result["properties"][field].get("description", "")
            result["properties"][field]["description"] = (
                meaning + " " + f"Write this entire {field} field only in {field_language}, "
                "including after a language switch. "
                "Do not copy a different language from history."
            )
    return result


class StructuredExecution:
    def __init__(
        self, workspace: Path, configuration: Callable[[], WorkspaceConfiguration]
    ) -> None:
        self.workspace = workspace
        self.configuration = configuration

    @cached_property
    def checkpoints(self) -> Any:
        from komaquill.adapters.checkpoints import Checkpoints
        from komaquill.adapters.database import database

        storage = database(self.workspace / ".artifacts/data/komaquill.sqlite3")
        return Checkpoints(storage.path)

    @cached_property
    def graph(self) -> Any:
        from komaquill.adapters.llm.graph import compile_request

        return compile_request(self.checkpoints.saver)

    def close(self) -> None:
        if "checkpoints" in self.__dict__:
            self.checkpoints.close()
        self.__dict__.pop("graph", None)

    @cached_property
    def settings(self) -> ConversationSettings:
        try:
            return self.configuration().conversation
        except (OSError, ValueError):
            raise KomaquillError(code="invalid_configuration", retryable=False) from None

    @cached_property
    def lock(self) -> FileLock:
        path = self.workspace / ".artifacts/state/.llm.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(path), timeout=0)

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
        self._record_usage(operation)

    def _record_usage(self, operation: UUID) -> None:
        from typing import cast

        from langchain_core.messages.ai import UsageMetadata, add_usage

        known: UsageMetadata | None = None
        count = 0
        unknown = 0
        directory = self.workspace / ".artifacts/data/conversation-calls" / str(operation)
        for path in sorted(directory.glob("*.json")):
            record = json.loads(path.read_bytes())
            count += 1
            usage = record.get("usage")
            if usage is None:
                unknown += 1
            else:
                known = add_usage(known, cast(UsageMetadata, usage))
        atomic_json(
            self.workspace / ".artifacts/data/conversation-usage" / f"{operation}.json",
            {
                "format_version": 1,
                "operation_id": str(operation),
                "call_count": count,
                "known_calls": count - unknown,
                "unknown_calls": unknown,
                "known_usage": known,
                "usage_complete": unknown == 0,
            },
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
        field_language: str | None = None,
        repair_attempts: int | None = None,
        output_tokens: int | None = None,
        trim: bool = True,
    ) -> T:
        profile = self.settings.llm_profiles.get(profile_id)
        if profile is None:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        if profile.provider == "fake":
            raise KomaquillError(code="fake_input_mismatch", retryable=False)
        if not permitted:
            raise KomaquillError(code="real_llm_not_allowed", retryable=False)
        from komaquill.adapters.database import database

        enabled = database(self.workspace / ".artifacts/data/komaquill.sqlite3").content.capture(
            operation
        )
        if enabled and urlsplit(profile.base_url or "").hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        system = json.dumps({"content_settings": {"nsfw_enabled": enabled}}) + "\n" + system
        try:
            from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
            from langsmith import tracing_context
            from openai import ContentFilterFinishReasonError, LengthFinishReasonError

            from komaquill.adapters.llm.context import ContextBudget, tokens
            from komaquill.adapters.llm.graph import WORKFLOW_VERSION, run_request
        except ImportError:
            raise KomaquillError(code="llm_dependencies_missing") from None
        context: list[BaseMessage] = [SystemMessage(content=system)]
        context.extend(
            AIMessage(content=text) if role == "assistant" else HumanMessage(content=text)
            for role, text in messages
        )
        wire_schema = response_schema(schema, field_language)
        output_limit = output_tokens or self.settings.max_output_tokens
        budget = ContextBudget(
            max_characters=self.settings.max_input_characters,
            window_tokens=self.settings.context_window_tokens,
            output_tokens=output_limit,
            repair_tokens=self.settings.repair_reserve_tokens,
            schema=json.dumps(wire_schema, ensure_ascii=False),
        )
        bound = self._model(profile).bind(
            max_completion_tokens=output_limit,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": wire_schema,
                },
            },
        )
        received: tuple[int, dict[str, Any]] | None = None
        invocation = hashlib.sha256(
            json.dumps(
                {"system": system, "schema": wire_schema, "messages": messages},
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        call_offset = 0

        def send(context: list[BaseMessage], attempt: int) -> str:
            nonlocal received
            record = {
                "format_version": 1,
                "status": "started",
                "invocation_id": str(invocation),
                "attempt": attempt,
                "workflow": {"engine": "langgraph", "version": WORKFLOW_VERSION},
                "model": profile.model,
                "profile": profile_id,
                "started_at": datetime.now(UTC).isoformat(),
                "schema": wire_schema,
                "instructions_sha256": hashlib.sha256(system.encode()).hexdigest(),
                "messages": [{"role": m.type, "content": m.content} for m in context],
                "max_output_tokens": output_limit,
                "temperature": profile.temperature,
                "reasoning_effort": profile.reasoning_effort,
                "usage": None,
                "input_tokens_estimated": tokens(context) + budget.schema_tokens,
                "context_window_tokens": budget.window_tokens,
                "repair_reserve_tokens": budget.repair_tokens,
            }
            index = call_offset + attempt
            path = (
                self.workspace
                / ".artifacts/data/conversation-calls"
                / str(operation)
                / f"{purpose}{index}.json"
            )
            if path.exists():
                previous = json.loads(path.read_bytes())
                if (
                    previous.get("invocation_id") != invocation
                    or previous.get("messages") != record["messages"]
                ):
                    raise KomaquillError(code="operation_conflict", retryable=False)
                if previous.get("status") == "response_received":
                    received = index, previous
                    if previous.get("failure_kind") in {
                        "refusal",
                        "truncated",
                        "content_filter",
                        "nontext",
                        "model_mismatch",
                    }:
                        raise KomaquillError(code="invalid_response", retryable=False)
                    return str(previous["response"])
                raise KomaquillError(
                    code=previous.get("error", "submission_unknown"), retryable=False
                )
            self.record(operation, index, record, purpose)
            started = time.monotonic()
            try:
                with tracing_context(enabled=False):
                    response = bound.invoke(context, config={"callbacks": []})
            except Exception as error:
                status = getattr(error, "status_code", None)
                code = (
                    "invalid_response"
                    if isinstance(error, (LengthFinishReasonError, ContentFilterFinishReasonError))
                    else "llm_request_invalid"
                    if status in {400, 422}
                    else ("authentication_failed" if status in {401, 403} else "llm_unavailable")
                )
                self.record(
                    operation,
                    index,
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
            actual = response.response_metadata.get("model_name") or response.response_metadata.get(
                "model"
            )
            failure_kind = (
                "model_mismatch"
                if profile.require_model_match and actual != profile.model
                else "refusal"
                if response.additional_kwargs.get("refusal")
                else "truncated"
                if response.response_metadata.get("finish_reason") == "length"
                else "content_filter"
                if response.response_metadata.get("finish_reason") == "content_filter"
                else "nontext"
                if not isinstance(response.content, str)
                else None
            )
            response_record = {
                **record,
                "status": "response_received",
                "response_model": actual,
                "finish_reason": response.response_metadata.get("finish_reason"),
                "response": response.content,
                "usage": response.usage_metadata,
                "elapsed_seconds": time.monotonic() - started,
                "failure_kind": failure_kind,
            }
            received = index, response_record
            self.record(operation, index, response_record, purpose)
            if failure_kind == "model_mismatch":
                raise KomaquillError(code="identity_mismatch", retryable=False)
            if failure_kind or not isinstance(response.content, str):
                raise KomaquillError(code="invalid_response", retryable=False)
            return response.content

        def parse(content: str) -> T:
            assert received is not None
            attempt, record = received
            try:
                value = schema.model_validate_json(content, strict=True)
                if validate:
                    validate(value)
            except ValueError as error:
                self.record(
                    operation,
                    attempt,
                    {
                        **record,
                        "validation": "rejected",
                        "failure_kind": "language"
                        if isinstance(error, ResponseLanguageError)
                        else "constraint"
                        if isinstance(error, ResponseConstraintError)
                        else "schema",
                    },
                    purpose,
                )
                raise
            self.record(operation, attempt, {**record, "validation": "accepted"}, purpose)
            return value

        def repair_prompt(invalid: ValueError) -> str:
            detail = (
                " The response uses the wrong language. "
                "Use the requested language for all visible fields."
                if isinstance(invalid, ResponseLanguageError) and invalid.field == "output"
                else f" The {invalid.field} field uses the wrong language. "
                "Translate that entire field; "
                "changing only the other field is insufficient."
                if isinstance(invalid, ResponseLanguageError)
                else ""
            )
            return (
                "Correct the previous response. Return valid JSON matching "
                "the supplied schema. Preserve the facts. "
                + correction
                + detail
                + (" " + invalid.correction if isinstance(invalid, ResponseConstraintError) else "")
            )

        try:
            with self.lock:
                settings_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "profile_id": profile_id,
                            "profile": profile.model_dump(exclude={"credential_ref", "key_env"}),
                            "limits": self.settings.model_dump(exclude={"llm_profiles"}),
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                settings_key = f"settings:{operation}"
                saved_settings = self.checkpoints.load(settings_key)
                if saved_settings is None:
                    self.checkpoints.save(settings_key, {"sha256": settings_hash})
                elif saved_settings != {"sha256": settings_hash}:
                    raise KomaquillError(code="operation_conflict", retryable=False)
                directory = self.workspace / ".artifacts/data/conversation-calls" / str(operation)
                records = [
                    (path, json.loads(path.read_bytes()))
                    for path in directory.glob("*.json")
                    if path.stem.startswith(purpose) and path.stem.removeprefix(purpose).isdecimal()
                ]
                retained = [
                    (path, value)
                    for path, value in records
                    if value.get("invocation_id") == invocation
                ]
                if retained:
                    call_offset = min(
                        int(path.stem.removeprefix(purpose)) - int(value["attempt"])
                        for path, value in retained
                    )
                    responses = [
                        (path, value)
                        for path, value in retained
                        if value.get("status") == "response_received"
                    ]
                    if responses:
                        path, value = max(
                            responses, key=lambda item: int(item[0].stem.removeprefix(purpose))
                        )
                        received = int(path.stem.removeprefix(purpose)), value
                else:
                    call_offset = (
                        max(
                            (int(path.stem.removeprefix(purpose)) for path, _ in records),
                            default=-1,
                        )
                        + 1
                    )
                return run_request(
                    context,
                    send,
                    parse,
                    repair_prompt,
                    repair_attempts=self.settings.repair_attempts
                    if repair_attempts is None
                    else repair_attempts,
                    budget=budget,
                    trim=trim,
                    graph=self.graph,
                    thread_id=f"{operation}:{purpose}:{invocation}",
                    schema=schema,
                )
        except Timeout:
            raise KomaquillError(code="operation_busy", retryable=True) from None
