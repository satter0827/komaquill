"""Render one scene at a time and derive progress from existing image histories."""

import secrets
import time
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

from komaquill.application.generation import (
    checked_seed,
    collect,
    generation_settings,
    submit_generation,
)
from komaquill.application.ports import GenerationBackend, Workspace
from komaquill.application.stories.image_inputs import scene_input
from komaquill.application.stories.ports import StoryRepository
from komaquill.domain.errors import KomaquillError, OperationStage
from komaquill.domain.images import MAX_SEED, PromptSource
from komaquill.domain.models import TERMINAL_RUN_STATES, JsonObject, Run, Session
from komaquill.domain.stories import SceneAttempts, StoryPlan, StoryRender
from komaquill.domain.workflow import digest


def create_render(
    repository: StoryRepository,
    workspace: Workspace,
    session: Session,
    story_id: UUID,
    image_profile: str,
    seed: int | None = None,
    *,
    candidates: int | None = None,
    negative: str | None = None,
    input_sources: tuple[PromptSource, ...] = (),
) -> StoryRender:
    """Capture a complete image-settings snapshot for a new independent rendering."""
    plan = repository.read(story_id)
    settings = generation_settings(
        workspace.configured(session.settings).settings(), image_profile, candidates
    )
    if image_profile not in settings.profiles:
        raise KomaquillError(code="invalid_configuration", stage="story_rendering")
    initial_seed = checked_seed(
        seed if seed is not None else secrets.randbits(63), stage=OperationStage.STORY_RENDERING
    )
    render = StoryRender(
        format_version=3,
        input_sources=input_sources,
        story_id=story_id,
        plan_sha256=digest(plan.model_dump(mode="json", exclude_unset=True)),
        image_profile=image_profile,
        settings=settings,
        scenes=[
            SceneAttempts(
                scene_id=scene.scene_id,
                seed=(initial_seed + index) % (MAX_SEED + 1),
                compiled_input=scene_input(plan, scene, settings.presets[image_profile], negative),
            )
            for index, scene in enumerate(plan.scenes)
        ],
    )
    repository.save_render(render)
    return render


def compatible(plan: StoryPlan, render: StoryRender, session: Session) -> None:
    """Reject condition changes rather than silently mixing rendering variants."""
    current = session.settings
    expected = render.settings
    profile = render.image_profile
    if (
        digest(plan.model_dump(mode="json", exclude_unset=True)) != render.plan_sha256
        or [scene.scene_id for scene in plan.scenes] != [scene.scene_id for scene in render.scenes]
        or current is None
        or profile not in current.profiles
        or any(
            current.assets.get(name) != expected.assets[name]
            for name in expected.presets[profile].asset_ids
        )
    ):
        raise KomaquillError(code="invalid_configuration", stage="story_rendering", retryable=False)


def latest_run(workspace: Workspace, scene: SceneAttempts) -> Run | None:
    if not scene.run_ids:
        return None
    try:
        return workspace.repository.read(scene.run_ids[-1])
    except FileNotFoundError:
        return None


def story_view(
    repository: StoryRepository,
    workspace: Workspace,
    story_id: UUID,
    render_id: UUID | None = None,
) -> JsonObject:
    """Expose a frontend-ready snapshot without fetching or rehashing images."""
    plan = repository.read(story_id)
    render = (
        repository.render(render_id)
        if render_id
        else next(
            iter(reversed(repository.renders(story_id))),
            None,
        )
    )
    if render is not None and render.story_id != story_id:
        raise KomaquillError(code="invalid_request", stage="story_rendering")
    if render is not None and (
        digest(plan.model_dump(mode="json", exclude_unset=True)) != render.plan_sha256
        or [scene.scene_id for scene in plan.scenes] != [scene.scene_id for scene in render.scenes]
    ):
        raise KomaquillError(code="storage_failed", stage="story_rendering")
    links = {item.scene_id: item for item in render.scenes} if render else {}
    scenes = []
    updated: list[datetime] = [plan.created_at]
    if render:
        updated.append(render.created_at)
    for scene in plan.scenes:
        link = links.get(scene.scene_id)
        run = latest_run(workspace, link) if link else None
        if run:
            updated.append(run.updated_at or run.created_at)
        images: list[JsonObject] = (
            [
                {
                    "key": f"runs/{run.run_id}/{name}",
                    "metadata": run.image_metadata[name].model_dump(mode="json")
                    if name in run.image_metadata
                    else None,
                }
                for name in run.images
            ]
            if run
            else []
        )
        candidates = [
            image
            for image in images
            if image["metadata"] and image["metadata"].get("role") == "candidate"
        ]
        scenes.append(
            {
                **scene.model_dump(mode="json", exclude_unset=True),
                **(
                    {"compiled_input": link.compiled_input.model_dump(mode="json")}
                    if link and link.compiled_input
                    else {}
                ),
                "state": run.state if run else "unstarted",
                "run_id": str(run.run_id) if run else None,
                "attempt_ids": [str(value) for value in link.run_ids] if link else [],
                "primary_image": candidates[0]
                if candidates
                else (
                    images[0]
                    if images and run and run.state == "saved" and run.output_spec is None
                    else None
                ),
                "candidates": candidates,
                "requested_candidates": run.output_spec.candidates
                if run and run.output_spec
                else (
                    render.settings.presets[render.image_profile].batch_size
                    if render and render.format_version >= 3
                    else None
                ),
                "images": images,
                "failure": run.failure.model_dump(mode="json") if run and run.failure else None,
            }
        )
    counts = dict(Counter(scene["state"] for scene in scenes))
    return {
        "format_version": 2,
        "story_id": str(story_id),
        "render_id": str(render.render_id) if render else None,
        "title": plan.title,
        "synopsis": plan.synopsis,
        "source": {
            "name": plan.request.source_name,
            "text": plan.request.source_text,
            "sha256": plan.request.source_sha256,
        },
        "image_count": len(plan.scenes),
        "requested_images": plan.request.image_count,
        "purpose": plan.request.purpose,
        "case_id": plan.request.case_id,
        "planner": {
            "profile": plan.planner.profile,
            "provider": plan.planner.provider,
            "configured_model": plan.planner.configured_model,
        },
        "characters": [
            item.model_dump(mode="json", exclude_unset=True) for item in plan.characters
        ],
        "supplements": plan.supplements,
        **(
            {"props": [prop.model_dump(mode="json") for prop in plan.props]}
            if plan.format_version == 2
            else {}
        ),
        "image_profile": render.image_profile if render else None,
        "updated_at": max(updated).isoformat(),
        "complete": counts.get("saved", 0) == len(plan.scenes),
        "finished": sum(counts.get(state, 0) for state in TERMINAL_RUN_STATES) == len(plan.scenes),
        "counts": counts,
        "scenes": scenes,
    }


def render_events(
    repository: StoryRepository,
    workspace: Workspace,
    client: GenerationBackend,
    session: Session,
    render_id: UUID,
    *,
    retry_failed: bool = False,
) -> Iterator[JsonObject]:
    """Pipeline bounded requests while using canonical histories for all state."""
    with repository.claim(render_id) as acquired:
        if not acquired:
            raise KomaquillError(code="invalid_request", stage="story_rendering", retryable=True)
        render = repository.render(render_id)
        plan = repository.read(render.story_id)
        compatible(plan, render, session)
        bound = workspace.configured(render.settings)
        bound_session = session.model_copy(update={"settings": render.settings})
        retries = {
            scene.scene_id
            for scene in render.scenes
            if retry_failed
            and (run := latest_run(bound, scene)) is not None
            and run.state in {"failed", "lost"}
        }
        # Reconcile existing requests before adding work after interruption.
        reconcile = any(
            (run := latest_run(bound, scene)) is not None and run.pending for scene in render.scenes
        )
        while True:
            render = repository.render(render_id)
            runs = {scene.scene_id: latest_run(bound, scene) for scene in render.scenes}
            active = [run for run in runs.values() if run is not None and run.pending]
            collected = False
            if reconcile:
                result = collect(bound, client, bound_session)
                yield story_view(repository, bound, render.story_id, render_id)
                if set(result.errors) & {run.run_id for run in active}:
                    raise KomaquillError(
                        code="collection_failed",
                        stage="story_rendering",
                        details={"render_id": str(render_id)},
                    )
                runs = {scene.scene_id: latest_run(bound, scene) for scene in render.scenes}
                active = [run for run in runs.values() if run is not None and run.pending]
                for run in active:
                    if (
                        datetime.now(UTC) - run.created_at
                    ).total_seconds() >= render.settings.generation_timeout:
                        raise KomaquillError(
                            code="generation_timeout", details={"run_id": str(run.run_id)}
                        )
                reconcile = False
                collected = True
            eligible = next(
                (
                    index
                    for index, scene in enumerate(render.scenes)
                    if runs[scene.scene_id] is None or scene.scene_id in retries
                ),
                None,
            )
            if eligible is None and not active:
                yield story_view(repository, bound, render.story_id, render_id)
                return
            if bound.stop_requested(bound_session) is not None:
                raise KomaquillError(code="stopping", stage="story_rendering", retryable=False)
            if (
                eligible is not None
                and len(active) < render.settings.story_max_in_flight
                and not any(run.state == "unknown" for run in active)
            ):
                link = render.scenes[eligible]
                compiled = link.compiled_input
                if compiled is None:
                    raise KomaquillError(
                        code="story_replan_required", stage="story_rendering", retryable=False
                    )
                if link.scene_id in retries or not link.run_ids:
                    link = link.model_copy(update={"run_ids": [*link.run_ids, uuid4()]})
                retries.discard(link.scene_id)
                values = list(render.scenes)
                values[eligible] = link
                render = render.model_copy(update={"scenes": values})
                repository.save_render(render)
                try:
                    submit_generation(
                        bound,
                        client,
                        bound_session,
                        compiled.prompt,
                        compiled.requested_negative,
                        link.seed,
                        render.image_profile,
                        run_id=link.run_ids[-1],
                        compiled_input=compiled,
                        input_sources=render.input_sources,
                    )
                except KomaquillError:
                    current = latest_run(bound, link)
                    yield story_view(repository, bound, render.story_id, render_id)
                    if current is None or current.state != "failed":
                        raise
                else:
                    yield story_view(repository, bound, render.story_id, render_id)
                continue
            reconcile = True
            if collected:
                time.sleep(render.settings.poll_interval)


def render_story(
    repository: StoryRepository,
    workspace: Workspace,
    client: GenerationBackend,
    session: Session,
    render_id: UUID,
    *,
    retry_failed: bool = False,
    notify: Callable[[JsonObject], None] | None = None,
) -> JsonObject:
    """Consume the shared iterator for callers that need a final snapshot."""
    result: JsonObject = {}
    for result in render_events(
        repository, workspace, client, session, render_id, retry_failed=retry_failed
    ):
        if notify:
            notify(result)
    return result
