"""Share generation, reconciliation and collection across CLI and browser requests."""

import time
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import TypeAdapter, ValidationError

from komaquill.application.ports import GenerationBackend, Workspace
from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import (
    FailureInfo,
    KomaquillError,
    OperationStage,
    SubmissionUnknown,
    failure_info,
)
from komaquill.domain.images import (
    CompiledImageInput,
    ImageSeed,
    PromptSource,
    compile_image_input,
    validate_inputs,
)
from komaquill.domain.materials import InputArtifact
from komaquill.domain.models import ImageRef, JsonObject, OutputSpec, Run, Session
from komaquill.domain.workflow import digest

_SEED_ADAPTER = TypeAdapter(ImageSeed)
_GENERATION_INPUTS = TypeAdapter(tuple[str, str | None, ImageSeed, tuple[str, ...]])


def checked_seed(seed: int, *, stage: OperationStage = OperationStage.GENERATION) -> int:
    """Reject out-of-range seeds before recording or sending any request."""
    try:
        return _SEED_ADAPTER.validate_python(seed)
    except ValidationError:
        raise KomaquillError(code="invalid_request", stage=stage, retryable=False) from None


def generation_settings(
    settings: RuntimeSettings,
    profile_name: str | None,
    candidates: int | None = None,
    parameters: dict[str, bool | int | float | str] | None = None,
) -> RuntimeSettings:
    """Freeze a request override without mutating the active session defaults."""
    name = profile_name or settings.profiles[0]
    if name not in settings.profiles:
        raise KomaquillError(code="invalid_request", stage="generation", retryable=False)
    profile = settings.presets[name]
    if parameters:
        try:
            profile = profile.configured_parameters(parameters)
        except ValueError:
            raise KomaquillError(
                code="invalid_request", stage="preparation", retryable=False
            ) from None
    if candidates is not None and profile.primary_output_node is None:
        raise KomaquillError(code="unsupported_batch", stage="generation", retryable=False)
    count = profile.batch_size if candidates is None else candidates
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 1 <= count <= settings.max_candidates
    ):
        raise KomaquillError(code="invalid_request", stage="generation", retryable=False)
    if count == profile.batch_size and not parameters:
        return settings
    return settings.model_copy(
        update={
            "presets": {**settings.presets, name: profile.model_copy(update={"batch_size": count})}
        }
    )


def output_spec(settings: RuntimeSettings | None, graph: JsonObject) -> OutputSpec | None:
    """Identify supported output roles only when the preset topology is known."""
    found: set[tuple[str, int]] = set()
    stages: dict[str, str] = {}
    if settings is None:
        return None
    for name in settings.profiles:
        profile = settings.presets[name]
        node = profile.primary_output_node
        if not node or set(graph) != set(profile.api):
            continue
        matches = True
        for key, expected in profile.api.items():
            actual = graph[key]
            inputs = actual.get("inputs") if isinstance(actual, dict) else None
            if (
                not isinstance(inputs, dict)
                or actual.get("class_type") != expected.get("class_type")
                or any(
                    inputs.get(k) != v
                    for k, v in expected.get("inputs", {}).items()
                    if isinstance(v, list)
                )
            ):
                matches = False
                break
        if not matches:
            continue
        for binding in profile.bindings:
            if (
                binding.source == "batch_size"
                and graph.get(node, {}).get("class_type") == "SaveImage"
            ):
                count = graph[binding.api_node].get("inputs", {}).get(binding.api_input)
                if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                    found.add((node, count))
                    stages = profile.output_stages
    return (
        OutputSpec(
            primary_node=next(iter(found))[0], candidates=next(iter(found))[1], stages=stages
        )
        if len(found) == 1
        else None
    )


@dataclass(frozen=True)
class Collection:
    """Describe the current collection, including unknown submissions."""

    saved: int
    pending: int
    failed: int
    errors: tuple[UUID | str, ...] = ()
    pending_ids: tuple[UUID, ...] = ()
    failure: FailureInfo | None = None
    observed_versions: tuple[tuple[UUID, str], ...] = ()


def new_run(project: Workspace, session: Session, run_id: UUID, graph: JsonObject) -> Run:
    """Create a history record containing reproducible generation conditions."""
    settings = session.settings
    enabled = (
        {asset for name in settings.profiles for asset in settings.presets[name].asset_ids}
        if settings
        else set()
    )
    known = (
        {name: asset for name, asset in settings.assets.items() if name in enabled}
        if settings and session.backend == "colab"
        else {}
    )
    known.update(project.prepared_assets(session))
    filenames: set[str] = set()
    for node in graph.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if isinstance(inputs, dict):
            filenames.update(
                value
                for key, value in inputs.items()
                if key.endswith(("_name", "_name1", "_name2", "_name3", "_file"))
                and isinstance(value, str)
            )
    used = {
        asset.reference: asset.sha256 for asset in known.values() if asset.reference in filenames
    }
    checkpoints = [
        asset.sha256
        for asset in known.values()
        if asset.role == "checkpoints" and asset.reference in filenames
    ]
    return Run(
        run_id=run_id,
        output_spec=output_spec(settings, graph),
        runtime_id=session.runtime_id,
        **project.identity(),
        remote_code_sha256=session.code_sha256,
        remote_source_revision=session.source_revision,
        comfy_revision=session.comfy_revision,
        frontend_version=session.frontend_version,
        model_sha256=checkpoints[0] if len(checkpoints) == 1 else None,
        asset_sha256=used,
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


def image_references(
    record: JsonObject, previous: Run | None = None
) -> list[tuple[str, ImageRef, str, int]]:
    """Validate output references without interpreting remote paths as local paths."""
    from pathlib import PurePosixPath

    outputs = record.get("outputs", {})
    if not isinstance(outputs, dict):
        raise KomaquillError(code="invalid_output", stage="history_validation")
    result: list[tuple[str, ImageRef, str, int]] = []
    retained = previous.image_metadata if previous else {}
    occupied = set(retained) | set(previous.images if previous else ())
    next_number = 1
    for node, output in outputs.items():
        if not isinstance(output, dict) or not isinstance(output.get("images", []), list):
            raise KomaquillError(code="invalid_output", stage="history_validation")
        for index, raw in enumerate(output.get("images", [])):
            reference = ImageRef.model_validate(raw)
            suffix = PurePosixPath(reference.filename).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}:
                raise KomaquillError(code="unsupported_image", stage="image_validation")
            matches = [
                name
                for name, metadata in retained.items()
                if (metadata.output_node, metadata.batch_index) == (str(node), index)
                or (metadata.output_node is None and metadata.source == reference)
            ]
            if len(matches) > 1:
                raise KomaquillError(code="invalid_output", stage="history_validation")
            if matches:
                name = matches[0]
            else:
                while any(PurePosixPath(name).stem == f"{next_number:04d}" for name in occupied):
                    next_number += 1
                name = f"images/{next_number:04d}{suffix}"
                occupied.add(name)
                next_number += 1
            result.append((name, reference, str(node), index))
    if not result:
        raise KomaquillError(code="missing_image", stage="image_collection")
    return result


def collect_images(
    project: Workspace,
    client: GenerationBackend,
    session: Session,
    run_id: UUID,
    record: JsonObject,
    *,
    finalize: bool = True,
) -> bool:
    """Fetch and validate outside the shared metadata lock, committing each checkpoint."""
    with collection_lock(project):
        references = image_references(record, project.repository.read(run_id))
    for name, reference, node, index in references:
        with collection_lock(project):
            run = project.repository.read(run_id)
            if (
                not run.pending and (finalize or run.state != "failed")
            ) or not project.accepts_results(session):
                return False
            previous = run.image_metadata.get(name)
            if previous is not None and (
                previous.source != reference
                or previous.output_node not in {None, node}
                or previous.batch_index not in {None, index}
            ):
                raise KomaquillError(
                    code="invalid_output", stage="history_validation", retryable=False
                )
        key = f"runs/{run_id}/{name}"
        content = project.artifacts_store.cached_image(key, previous)
        if content is None:
            content = client.image(reference)
        metadata = project.artifacts_store.inspect_image(content, reference)
        metadata.output_node = node
        metadata.batch_index = index
        metadata.role = (
            ("candidate" if node == run.output_spec.primary_node else "auxiliary")
            if run.output_spec
            else "unknown"
        )
        metadata.stage = (
            ("final" if metadata.role == "candidate" else run.output_spec.stages.get(node))
            if run.output_spec
            else None
        )
        with collection_lock(project):
            run = project.repository.read(run_id)
            if (
                not run.pending and (finalize or run.state != "failed")
            ) or not project.accepts_results(session):
                return False
            project.artifacts_store.write(key, content)
            if name not in run.images:
                run.images.append(name)
            if run.format_version >= 5:
                retained = run.image_metadata.get(name)
                metadata.saved_at = (
                    retained.saved_at if retained and retained.saved_at else datetime.now(UTC)
                )
                if metadata.role == "candidate" and run.first_candidate_available_at is None:
                    run.first_candidate_available_at = metadata.saved_at
            run.image_metadata[name] = metadata
            project.repository.save(run)
    with collection_lock(project):
        run = project.repository.read(run_id)
        if not finalize:
            if run.state == "failed" and project.accepts_results(session):
                run.collection_failure = None
                project.repository.save(run)
            return False
        if not run.pending or not project.accepts_results(session):
            return False
        if (
            run.output_spec is not None
            and sum(node == run.output_spec.primary_node for _, _, node, _ in references)
            != run.output_spec.candidates
        ):
            raise KomaquillError(
                code="invalid_output",
                stage="image_collection",
                retryable=True,
                details={"run_id": str(run_id)},
            )
        run.state = "saved"
        run.error = None
        run.failure = None
        project.repository.save(run)
    return True


def collect(
    project: Workspace, client: GenerationBackend, session: Session, only: UUID | None = None
) -> Collection:
    """Continue independent requests without holding a shared lock over network waits."""
    project = project.configured(session.settings)
    verify_runtime(client, session)
    records: dict[str, JsonObject] = {row[1]: {"prompt": row} for row in client.queue()}
    records.update(client.history())
    with collection_lock(project):
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
        if identity not in runs and prompt_id in session.excluded_prompt_ids:
            continue
        related.add(identity)
        with project.repository.claim(identity) as acquired:
            if not acquired:
                continue
            try:
                with collection_lock(project):
                    if not project.accepts_results(session):
                        continue
                    try:
                        run = project.repository.read(identity)
                    except FileNotFoundError:
                        graph = prompt[2] if len(prompt) > 2 and isinstance(prompt[2], dict) else {}
                        run = new_run(project, session, identity, graph)
                    owns_request = (
                        session.backend == "external" and request_id(prompt) == run.run_id
                    )
                    if run.runtime_id != session.runtime_id and not owns_request:
                        continue
                    terminal_failure = run.state == "failed" and bool(record.get("outputs"))
                    if terminal_failure and only is None and run.collection_failure is None:
                        references = image_references(record, run)
                        terminal_failure = any(
                            name not in run.image_metadata
                            or run.image_metadata[name].source != reference
                            for name, reference, _, _ in references
                        )
                    if not run.pending and not terminal_failure:
                        continue
                    run.prompt_id = prompt_id
                    if run.pending:
                        run.state = "queued"
                    project.repository.save(run)
                    if len(prompt) < 3:
                        raise KomaquillError(
                            code="invalid_response", stage="history_validation", retryable=False
                        )
                    project.repository.conditions(run, prompt)
                    status = record.get("status", {})
                    if not isinstance(status, dict):
                        raise KomaquillError(
                            code="invalid_response", stage="history_validation", retryable=False
                        )
                    generation_times(run, status)
                    if status.get("status_str") == "error":
                        run.state = "failed"
                        run.error = "generation_failed"
                        run.failure = failure_info(
                            KomaquillError(code="generation_failed", retryable=False), "generation"
                        )
                    project.repository.save(run)
                    completed = status.get("completed") and run.pending
                    failed_outputs = run.state == "failed" and bool(record.get("outputs"))
                if (completed or failed_outputs) and collect_images(
                    project, client, session, identity, record, finalize=bool(completed)
                ):
                    saved += 1
            except (KomaquillError, OSError, ValidationError) as error:
                errors.append(identity)
                with collection_lock(project):
                    try:
                        current = project.repository.read(identity)
                    except (OSError, ValueError, KomaquillError):
                        continue
                    if current.state == "failed" and project.accepts_results(session):
                        current.collection_failure = failure_info(error, "image_collection")
                        project.repository.save(current)
                        continue
                    if not current.pending or not project.accepts_results(session):
                        continue
                    stage = (
                        "history_validation"
                        if isinstance(error, ValidationError)
                        else error.stage
                        if isinstance(error, KomaquillError) and error.stage != "operation"
                        else "image_collection"
                    )
                    current.failure = failure_info(error, stage)
                    current.error = "collection_failed"
                    with suppress(OSError):
                        project.repository.save(current)
    with collection_lock(project):
        final_unreadable: list[UUID | str] = []
        selected = [
            run
            for run in project.repository.load(final_unreadable)
            if (run.runtime_id == session.runtime_id or run.run_id in related)
            and (only is None or run.run_id == only)
        ]
    errors.extend(
        identity
        for identity in final_unreadable
        if identity not in errors and (only is None or identity == only)
    )
    pending = tuple(run.run_id for run in selected if run.pending)
    return Collection(
        saved=saved,
        pending=len(pending),
        failed=sum(run.state == "failed" for run in selected),
        errors=tuple(errors),
        pending_ids=pending,
        observed_versions=tuple(
            (run.run_id, digest(run.model_dump(mode="json"))) for run in selected
        ),
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


def submit_generation(
    project: Workspace,
    client: GenerationBackend,
    session: Session,
    prompt: str,
    negative: str | None,
    seed: int,
    profile_name: str | None = None,
    *,
    run_id: UUID | None = None,
    compiled_input: CompiledImageInput | None = None,
    subjects: tuple[str, ...] = (),
    candidates: int | None = None,
    input_sources: tuple[PromptSource, ...] = (),
    inputs: dict[str, InputArtifact] | None = None,
    parameters: dict[str, bool | int | float | str] | None = None,
) -> Run:
    """Record before submission and return without waiting for image persistence."""
    try:
        prompt, negative, seed, subjects = _GENERATION_INPUTS.validate_python(
            (prompt, negative, seed, subjects)
        )
    except ValidationError:
        raise KomaquillError(code="invalid_request", stage="generation", retryable=False) from None
    if not prompt.strip():
        raise KomaquillError(code="invalid_request", stage="generation", retryable=False)
    settings = generation_settings(
        project.configured(session.settings).settings(), profile_name, candidates, parameters
    )
    project = project.configured(settings)
    session = session.model_copy(update={"settings": settings})
    verify_runtime(client, session)
    run_id = run_id or uuid4()
    if compiled_input is None:
        settings = project.settings()
        selected = profile_name or settings.profiles[0]
        if selected not in settings.profiles:
            raise KomaquillError(code="invalid_request", retryable=False)
        try:
            compiled_input = compile_image_input(
                settings.presets[selected], prompt, negative, subjects, inputs
            )
        except ValidationError:
            raise KomaquillError(
                code="invalid_request", stage="generation", retryable=False
            ) from None
    selected = profile_name or settings.profiles[0]
    try:
        validate_inputs(settings.presets[selected], compiled_input.inputs)
    except ValueError:
        raise KomaquillError(code="invalid_request", stage="preparation", retryable=False) from None
    contents = {}
    for name, item in compiled_input.inputs.items():
        contents[name] = project.artifacts_store.input_content(item)
    for name, item in compiled_input.inputs.items():
        client.upload_image(item, contents[name])
    graph, ui = project.prepare(
        prompt, negative, seed, str(run_id), profile_name, compiled_input=compiled_input
    )
    if session.backend == "external":
        graph, ui = client.resolve_models(graph, ui)
    run = new_run(project, session, run_id, graph)
    run.compiled_input = compiled_input
    run.input_sha256 = {item.reference: item.sha256 for item in compiled_input.inputs.values()}
    run.input_sources = input_sources
    run.state = "unknown"
    with collection_lock(project):
        if project.stop_requested(session) is not None:
            raise KomaquillError(code="stopping", retryable=False)
        try:
            project.repository.read(run_id)
        except FileNotFoundError:
            pass
        else:
            raise KomaquillError(code="invalid_request", stage="submission", retryable=False)
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
        error.details.setdefault("run_id", str(run_id))
        raise
    with collection_lock(project):
        run = project.repository.read(run_id)
        run.prompt_id = prompt_id
        if run.pending:
            run.state = "queued"
        project.repository.save(run)
    return run


def wait_generation(
    project: Workspace, client: GenerationBackend, session: Session, run_id: UUID
) -> Run:
    """Collect a recorded request without submitting it again."""
    project = project.configured(session.settings)
    settings = project.settings()
    deadline = time.monotonic() + settings.generation_timeout
    while time.monotonic() < deadline:
        result = collect(project, client, session, only=run_id)
        run = project.repository.read(run_id)
        if run.state == "saved":
            return run
        if run.state in {"failed", "lost"}:
            raise KomaquillError(
                code=run.failure.code if run.failure else "generation_failed",
                stage=run.failure.stage if run.failure else "generation",
                details={"run_id": str(run_id)},
                retryable=False,
            )
        if run_id in result.errors:
            raise KomaquillError(code="collection_failed", details={"run_id": str(run_id)})
        time.sleep(settings.poll_interval)
    raise KomaquillError(code="generation_timeout", details={"run_id": str(run_id)})


def generate(
    project: Workspace,
    client: GenerationBackend,
    session: Session,
    prompt: str,
    negative: str | None,
    seed: int,
    profile_name: str | None = None,
    *,
    subjects: tuple[str, ...] = (),
    candidates: int | None = None,
    input_sources: tuple[PromptSource, ...] = (),
    inputs: dict[str, InputArtifact] | None = None,
    parameters: dict[str, bool | int | float | str] | None = None,
) -> Run:
    """Share request submission and recovery with story generation."""
    run = submit_generation(
        project,
        client,
        session,
        prompt,
        negative,
        seed,
        profile_name,
        subjects=subjects,
        candidates=candidates,
        input_sources=input_sources,
        inputs=inputs,
        parameters=parameters,
    )
    return wait_generation(project, client, session, run.run_id)
