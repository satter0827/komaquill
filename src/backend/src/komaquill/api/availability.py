"""Observe only the connections required by the selected operation."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from keyring.errors import KeyringError

from komaquill.adapters.llm.execution import StructuredExecution
from komaquill.adapters.llm.images import needs_image_llm
from komaquill.adapters.llm_credentials import api_key
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.availability import Availability, AvailabilityRequest, ConnectionObservation
from komaquill.conversations.models import Conversation, Operation
from komaquill.domain.errors import ErrorCode, KomaquillError
from komaquill.domain.models import Run
from komaquill.library.models import CreationRequest


@dataclass(frozen=True)
class _ObservedProbe:
    sequence: int
    result: ConnectionObservation


class AvailabilityService:
    def __init__(
        self,
        project: RuntimeWorkspace,
        execution: StructuredExecution,
        conversation: Callable[[UUID], Conversation],
        creation_source: Callable[[CreationRequest], tuple[str, UUID | None]],
        image: Callable[[UUID], Run],
        operation: Callable[[UUID], Operation],
    ) -> None:
        self.project, self.execution = project, execution
        self.conversation, self.creation_source = conversation, creation_source
        self.image, self.operation = image, operation
        self._observations: dict[tuple[str, str], _ObservedProbe] = {}
        self._sequence = 0
        self._lock = Lock()

    def check(self, request: AvailabilityRequest, *, refresh: bool = False) -> Availability:
        required: list[tuple[str, str]] = []
        if request.kind == "conversation":
            assert request.identity is not None
            conversation = self.conversation(request.identity)
            required.append(("llm", conversation.definition.llm_profile))
        elif request.kind == "creation":
            assert request.creation is not None
            source, _ = self.creation_source(request.creation)
            required = self.creation_dependencies(request.creation, source)
        elif request.kind == "image":
            required.append(("comfy", "current"))
            if any(
                needs_image_llm(value)
                for value in (request.prompt, *request.subjects, request.negative or "")
            ):
                required.append(("llm", request.llm_profile))
        elif request.kind == "collection":
            assert request.identity is not None
            run = self.image(request.identity)
            if run.state != "saved":
                required.append(("comfy", str(run.runtime_id)))
        elif request.kind == "resume":
            assert request.identity is not None
            operation = self.operation(request.identity)
            if not operation.can_resume:
                raise KomaquillError(code="operation_conflict", retryable=False)
        connections = [self.observe(kind, target, refresh=refresh) for kind, target in required]
        return Availability(
            ready=all(c.state == "ready" for c in connections), connections=connections
        )

    def creation_dependencies(self, request: CreationRequest, source: str) -> list[tuple[str, str]]:
        if request.kind.endswith("_draft"):
            return [("llm", request.llm_profile)]
        result = [("comfy", "current")]
        if needs_image_llm(source, compose=True, purpose=request.kind):
            result.append(("llm", request.llm_profile))
        return result

    def require(self, request: AvailabilityRequest) -> None:
        result = self.check(request)
        for connection in result.connections:
            if connection.state != "ready":
                raise KomaquillError(
                    code=connection.reason or ErrorCode.CONNECTION_FAILED,
                    stage="connection",
                    retryable=True,
                )

    def require_conversation(self, identity: UUID, permitted: bool) -> None:
        conversation = self.conversation(identity)
        self._require_permission(conversation.definition.llm_profile, permitted)
        self.require(AvailabilityRequest(kind="conversation", identity=identity))

    def require_creation(self, request: CreationRequest, source: str) -> None:
        for kind, target in self.creation_dependencies(request, source):
            if kind == "llm":
                self._require_permission(target, request.allow_real_llm)
            observed = self.observe(kind, target)
            if observed.state != "ready":
                raise KomaquillError(
                    code=observed.reason or ErrorCode.CONNECTION_FAILED,
                    stage="connection",
                    retryable=True,
                )

    def _require_permission(self, profile_id: str, permitted: bool) -> None:
        profile = self.execution.settings.llm_profiles.get(profile_id)
        if profile is not None and profile.provider != "fake" and not permitted:
            raise KomaquillError(code="real_llm_not_allowed", retryable=False)

    def observe(self, kind: str, target: str, *, refresh: bool = False) -> ConnectionObservation:
        settings = self.execution.settings
        now = datetime.now(UTC)
        cache_target = target
        if kind == "comfy":
            from komaquill.adapters.storage import load_session

            try:
                session = load_session(self.project)
                cache_target += str(session.runtime_id) + session.endpoint
            except (OSError, ValueError):
                cache_target += ":unconfigured"
        key = (kind, cache_target)
        with self._lock:
            known = self._observations.get(key)
            if not refresh and known and known.result.expires_at > now:
                return known.result
            self._sequence += 1
            sequence = self._sequence
        reason = None
        try:
            if kind == "llm":
                self._llm(target, settings.connection_probe_timeout)
            else:
                self._comfy(target, settings.connection_probe_timeout)
        except KomaquillError as error:
            reason = (
                ErrorCode.IMAGE_BACKEND_UNAVAILABLE
                if kind == "comfy" and error.code == ErrorCode.OPERATION_FAILED
                else error.code
            )
        except KeyringError:
            reason = ErrorCode.AUTHENTICATION_FAILED
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            reason = (
                ErrorCode.LLM_UNAVAILABLE if kind == "llm" else ErrorCode.IMAGE_BACKEND_UNAVAILABLE
            )
        observed = datetime.now(UTC)
        result = ConnectionObservation(
            kind="llm" if kind == "llm" else "comfy",
            target=target,
            state="ready" if reason is None else "unavailable",
            reason=reason,
            observed_at=observed,
            expires_at=observed + timedelta(seconds=settings.connection_observation_ttl),
        )
        with self._lock:
            newer = self._observations.get(key)
            if newer is not None and newer.sequence > sequence:
                return newer.result
            self._observations[key] = _ObservedProbe(sequence, result)
        return result

    def _llm(self, identity: str, timeout: float) -> None:
        profile = self.execution.settings.llm_profiles.get(identity)
        if profile is None:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        if profile.provider == "fake":
            return
        url = urlsplit(profile.base_url or "")
        if url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise KomaquillError(code="invalid_configuration", retryable=False)
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"Authorization": "Bearer " + api_key(profile)},
        ) as http:
            if profile.token_counting == "lmstudio":
                from importlib.util import find_spec

                if find_spec("lmstudio") is None:
                    raise KomaquillError(code="llm_dependencies_missing", retryable=False)
                response = http.get(f"{url.scheme}://{url.netloc}/api/v1/models")
                response.raise_for_status()
                instances = [
                    instance
                    for model in response.json()["models"]
                    if model["type"] == "llm"
                    for instance in model["loaded_instances"]
                ]
                if not any(
                    instance["id"] == profile.model and instance["config"]["context_length"] > 0
                    for instance in instances
                ):
                    raise KomaquillError(code="llm_unavailable", retryable=False)
            else:
                response = http.get((profile.base_url or "").rstrip("/") + "/models")
                response.raise_for_status()
                if not any(model["id"] == profile.model for model in response.json()["data"]):
                    raise KomaquillError(code="identity_mismatch", retryable=False)

    def _comfy(self, target: str, timeout: float) -> None:
        from komaquill.adapters.storage import stop_request
        from komaquill.images.generation import verify_runtime
        from komaquill.runtime.connection import connected

        with connected(self.project) as (backend, session):
            if target == "current" and stop_request(self.project, session) is not None:
                raise KomaquillError(code="stopping", retryable=False)
            if target != "current" and target != str(session.runtime_id):
                raise KomaquillError(code="identity_mismatch", retryable=False)
            backend.http.timeout = httpx.Timeout(timeout)
            backend.json("/system_stats")
            verify_runtime(backend, session)
