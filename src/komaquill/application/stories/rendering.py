"""Render one scene at a time and derive progress from existing image histories."""

import secrets
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime
from uuid import UUID, uuid4

from komaquill.application.generation import submit_generation, wait_generation
from komaquill.application.ports import GenerationBackend, Workspace
from komaquill.application.stories.ports import StoryRepository
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import JsonObject, Run, Session
from komaquill.domain.stories import SceneAttempts, StoryPlan, StoryRender, image_prompt
from komaquill.domain.workflow import digest


def create_render(
    repository: StoryRepository,
    workspace: Workspace,
    session: Session,
    story_id: UUID,
    image_profile: str,
    seed: int | None = None,
) -> StoryRender:
    """Capture a complete image-settings snapshot for a new independent rendering."""
    plan = repository.read(story_id)
    settings = workspace.configured(session.settings).settings()
    if image_profile not in settings.profiles:
        raise KomaquillError(code="invalid_configuration", stage="story_rendering")
    initial_seed = seed if seed is not None else secrets.randbits(63)
    render = StoryRender(
        story_id=story_id,
        plan_sha256=digest(plan.model_dump(mode="json", exclude_unset=True)),
        image_profile=image_profile,
        settings=settings,
        scenes=[
            SceneAttempts(scene_id=scene.scene_id, seed=(initial_seed + index) % (2**64))
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
        images = (
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
        scenes.append(
            {
                **scene.model_dump(mode="json"),
                "state": run.state if run else "unstarted",
                "run_id": str(run.run_id) if run else None,
                "attempt_ids": [str(value) for value in link.run_ids] if link else [],
                "primary_image": images[0] if images and run and run.state == "saved" else None,
                "images": images,
                "failure": run.failure.model_dump(mode="json") if run and run.failure else None,
            }
        )
    counts = dict(Counter(scene["state"] for scene in scenes))
    return {
        "format_version": 1,
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
        "characters": [item.model_dump(mode="json") for item in plan.characters],
        "supplements": plan.supplements,
        "image_profile": render.image_profile if render else None,
        "updated_at": max(updated).isoformat(),
        "complete": counts.get("saved", 0) == len(plan.scenes),
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
    """Keep one in-flight scene and preserve links before any submission."""
    with repository.claim(render_id) as acquired:
        if not acquired:
            raise KomaquillError(code="invalid_request", stage="story_rendering", retryable=True)
        render = repository.render(render_id)
        plan = repository.read(render.story_id)
        compatible(plan, render, session)
        bound = workspace.configured(render.settings)
        bound_session = session.model_copy(update={"settings": render.settings})
        for index, scene in enumerate(plan.scenes):
            render = repository.render(render_id)
            link = render.scenes[index]
            run = latest_run(bound, link)
            if run is not None and run.state == "saved":
                continue
            if run is not None and run.state in {"failed", "lost"}:
                if not retry_failed:
                    continue
                run = None
                link = link.model_copy(update={"run_ids": [*link.run_ids, uuid4()]})
            elif run is None and not link.run_ids:
                link = link.model_copy(update={"run_ids": [uuid4()]})
            if bound.stop_requested(bound_session) is not None:
                raise KomaquillError(code="stopping", stage="story_rendering", retryable=False)
            values = list(render.scenes)
            values[index] = link
            render = render.model_copy(update={"scenes": values})
            repository.save_render(render)
            try:
                if run is None:
                    run = submit_generation(
                        bound,
                        client,
                        bound_session,
                        image_prompt(plan, scene),
                        None,
                        link.seed,
                        render.image_profile,
                        run_id=link.run_ids[-1],
                    )
                yield story_view(repository, bound, render.story_id, render_id)
                if run.state != "saved":
                    wait_generation(bound, client, bound_session, run.run_id)
            except KomaquillError:
                current = latest_run(bound, link)
                yield story_view(repository, bound, render.story_id, render_id)
                if current is None or current.state != "failed":
                    raise
            else:
                yield story_view(repository, bound, render.story_id, render_id)
        yield story_view(repository, bound, render.story_id, render_id)


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
