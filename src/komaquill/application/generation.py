"""Share generation, reconciliation and collection across CLI and browser requests."""

import time
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ValidationError

from komaquill.application.ports import GenerationBackend, Workspace
from komaquill.domain.errors import FailureInfo, KomaquillError, SubmissionUnknown, failure_info
from komaquill.domain.models import JsonObject, Run, Session
from komaquill.domain.workflow import digest


@dataclass(frozen=True)
class Collection:
    """Describe the current collection, including unknown submissions."""

    saved: int
    pending: int
    failed: int
    errors: tuple[UUID | str, ...] = ()
    pending_ids: tuple[UUID, ...] = ()
    failure: FailureInfo | None = None


def new_run(project: Workspace, session: Session, run_id: UUID, graph: JsonObject) -> Run:
    """Create a history record containing reproducible generation conditions."""
    return Run(
        run_id=run_id,
        runtime_id=session.runtime_id,
        **project.identity(),
        remote_code_sha256=session.code_sha256,
        remote_source_revision=session.source_revision,
        comfy_revision=session.comfy_revision,
        frontend_version=session.frontend_version,
        model_sha256=session.model_sha256,
        workflow_sha256=digest(graph),
    )


def request_id(prompt: list[Any]) -> UUID | None:
    """Read a local request identity from ComfyUI extra_data."""
    try:
        return UUID(str(prompt[3]["komaquill"]["run_id"]))
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def generation_times(run: Run, status: JsonObject) -> None:
    """Record server timestamps only when ComfyUI reports valid execution events."""
    messages = status.get("messages", [])
    if not isinstance(messages, list):
        return
    for row in messages:
        if not isinstance(row, (list, tuple)) or len(row) != 2 or not isinstance(row[1], dict):
            continue
        value = row[1].get("timestamp")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            timestamp = datetime.fromtimestamp(value / 1000, UTC)
        except (OverflowError, OSError, ValueError):
            continue
        if row[0] == "execution_start":
            run.generation_started_at = timestamp
        elif row[0] in {"execution_success", "execution_error", "execution_interrupted"}:
            run.generation_completed_at = timestamp


def verify_runtime(client: GenerationBackend, session: Session) -> None:
    """Verify runtime identity for managed endpoints only."""
    if session.backend == "colab" and str(client.runtime().get("runtime_id")) != str(
        session.runtime_id
    ):
        raise KomaquillError(code="identity_mismatch", stage="connection", retryable=False)


def collection_lock(project: Workspace) -> AbstractContextManager[Any]:
    """Use the repository transaction boundary for recording and collection."""
    return project.repository.lock()


def collect(
    project: Workspace, client: GenerationBackend, session: Session, only: UUID | None = None
) -> Collection:
    """Observe queue and history, then collect each request independently."""
    project = project.configured(session.settings)
    with collection_lock(project):
        verify_runtime(client, session)
        records: dict[str, JsonObject] = {row[1]: {"prompt": row} for row in client.queue()}
        records.update(client.history())
        unreadable: list[UUID | str] = []
        runs = {run.run_id: run for run in project.repository.load(unreadable)}
        known_prompts = {
            run.prompt_id: run.run_id
            for run in runs.values()
            if run.runtime_id == session.runtime_id and run.prompt_id is not None
        }
        if only is not None and only not in runs and only not in unreadable:
            raise KomaquillError(code="request_not_found", retryable=False)
        related: set[UUID] = set()
        errors = [identity for identity in unreadable if only is None or identity == only]
        saved = 0
        for prompt_id, raw_record in records.items():
            record = raw_record if isinstance(raw_record, dict) else {}
            raw_prompt = record.get("prompt")
            prompt = raw_prompt if isinstance(raw_prompt, list) else []
            identity = (
                request_id(prompt)
                or known_prompts.get(prompt_id)
                or uuid5(NAMESPACE_URL, f"komaquill:{session.runtime_id}:{prompt_id}")
            )
            if identity in unreadable or (only is not None and identity != only):
                continue
            run = runs.get(identity)
            if run is None:
                if prompt_id in session.excluded_prompt_ids:
                    continue
                graph = prompt[2] if len(prompt) > 2 and isinstance(prompt[2], dict) else {}
                run = new_run(project, session, identity, graph)
                runs[identity] = run
            owns_request = session.backend == "external" and request_id(prompt) == run.run_id
            if run.runtime_id != session.runtime_id and not owns_request:
                continue
            related.add(run.run_id)
            if not run.pending:
                continue
            try:
                run.prompt_id = prompt_id
                run.state = "queued"
                project.repository.save(run)
                if not isinstance(prompt, list) or len(prompt) < 3:
                    raise KomaquillError(
                        code="invalid_response", stage="history_validation", retryable=False
                    )
                project.repository.conditions(run, prompt)
                status = record.get("status", {})
                if not isinstance(status, dict):
                    raise KomaquillError(
                        code="invalid_response", stage="history_validation", retryable=False
                    )
                if status.get("status_str") == "error":
                    run.state = "failed"
                    run.error = "generation_failed"
                    run.failure = failure_info(
                        KomaquillError("", code="generation_failed", retryable=False), "generation"
                    )
                    generation_times(run, status)
                    project.repository.save(run)
                    continue
                generation_times(run, status)
                if not status.get("completed"):
                    project.repository.save(run)
                    continue
                run.images = project.repository.images(client, run, record)
                run.state = "saved"
                run.error = None
                run.failure = None
                project.repository.save(run)
                saved += 1
            except (KomaquillError, OSError, ValidationError) as error:
                errors.append(run.run_id)
                run.state = "queued"
                stage = "storage"
                if isinstance(error, ValidationError):
                    stage = "history_validation"
                elif isinstance(error, KomaquillError):
                    stage = error.stage if error.stage != "operation" else "image_collection"
                run.failure = failure_info(error, stage)
                run.error = "collection_failed"
                with suppress(OSError):
                    project.repository.save(run)
        selected = [
            run
            for run in runs.values()
            if (run.runtime_id == session.runtime_id or run.run_id in related)
            and (only is None or run.run_id == only)
        ]
        pending = tuple(run.run_id for run in selected if run.pending)
        return Collection(
            saved=saved,
            pending=len(pending),
            failed=sum(run.state == "failed" for run in selected),
            errors=tuple(errors),
            pending_ids=pending,
        )


def mark_lost(project: Workspace, runtime_id: UUID) -> tuple[UUID, ...]:
    """Finalize pending requests after confirming the owned runtime has ended."""
    lost: list[UUID] = []
    with collection_lock(project):
        unreadable: list[UUID | str] = []
        for run in project.repository.load(unreadable):
            if run.runtime_id == runtime_id and run.pending:
                run.state = "lost"
                run.error = "runtime_ended"
                run.failure = failure_info(
                    KomaquillError("", code="runtime_ended", retryable=False), "collection"
                )
                project.repository.save(run)
                lost.append(run.run_id)
        if unreadable:
            raise KomaquillError(code="storage_failed", stage="history_validation")
    return tuple(lost)


def generate(
    project: Workspace,
    client: GenerationBackend,
    session: Session,
    prompt: str,
    negative: str,
    seed: int,
) -> Run:
    """Record before submission and wait until local persistence completes."""
    project = project.configured(session.settings)
    verify_runtime(client, session)
    run_id = uuid4()
    graph, ui = project.prepare(prompt, negative, seed, str(run_id))
    run = new_run(project, session, run_id, graph)
    run.state = "unknown"
    with collection_lock(project):
        if project.stop_requested(session) is not None:
            raise KomaquillError(code="stopping", retryable=False)
        project.repository.prepare(run, graph, ui)
        project.repository.save(run)
    try:
        prompt_id = client.submit(graph, ui, str(run_id))
    except SubmissionUnknown as exc:
        with collection_lock(project):
            run = project.repository.read(run_id)
            if run.pending:
                run.failure = failure_info(
                    KomaquillError("", code="submission_unknown"), "submission"
                )
                project.repository.save(run)
        raise SubmissionUnknown(code="submission_unknown", details={"run_id": str(run_id)}) from exc
    except KomaquillError as error:
        with collection_lock(project):
            run = project.repository.read(run_id)
            if run.pending:
                run.state = "failed"
                run.error = "submission_failed"
                run.failure = failure_info(error, "submission")
                project.repository.save(run)
        raise
    with collection_lock(project):
        run = project.repository.read(run_id)
        run.prompt_id = prompt_id
        if run.pending:
            run.state = "queued"
        project.repository.save(run)
    settings = project.settings()
    deadline = time.monotonic() + settings.generation_timeout
    while time.monotonic() < deadline:
        result = collect(project, client, session, only=run_id)
        run = project.repository.read(run_id)
        if run.state == "saved":
            return run
        if run.state in {"failed", "lost"}:
            raise KomaquillError(
                code=run.failure.code if run.failure else "generation_failed", retryable=False
            )
        if run_id in result.errors:
            raise KomaquillError(code="collection_failed", details={"run_id": str(run_id)})
        time.sleep(settings.poll_interval)
    raise KomaquillError(code="generation_timeout", details={"run_id": str(run_id)})
