"""Associate existing image requests with canonical story scenes."""

from uuid import UUID

from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import JsonObject
from komaquill.domain.workflow import digest


def story_associations(project: RuntimeWorkspace) -> tuple[dict[str, list[JsonObject]], list[str]]:
    """Keep corrupt optional story records from hiding valid image evidence."""
    from komaquill.adapters.archive import ArchiveRepository

    repository = ArchiveRepository(project)
    result: dict[str, list[JsonObject]] = {}
    unreadable = []
    for path in repository.root.glob("*/renders/*/render.json"):
        try:
            render = repository.render(UUID(path.parent.name))
            plan = repository.read(render.story_id)
            if render.plan_sha256 != digest(plan.model_dump(mode="json", exclude_unset=True)):
                raise ValueError("The plan changed after rendering.")
            scenes = {scene.scene_id: scene for scene in plan.scenes}
            pending: dict[str, JsonObject] = {}
            for link in render.scenes:
                scene = scenes[link.scene_id]
                context = {
                    "story_id": str(render.story_id),
                    "render_id": str(render.render_id),
                    "scene_id": str(scene.scene_id),
                    "position": scene.position,
                    "title": scene.title,
                    "narrative": scene.narrative,
                    "source_sha256": plan.request.source_sha256,
                    "source_refs": scene.source_refs,
                    "plan_sha256": render.plan_sha256,
                    "purpose": plan.request.purpose,
                    "case_id": plan.request.case_id,
                    "planner_provider": plan.planner.provider,
                    "planner_model": plan.planner.configured_model,
                }
                pending.update({str(identity): context for identity in link.run_ids})
        except (OSError, ValueError, KeyError, KomaquillError):
            unreadable.append(path.parent.name)
            continue
        for identity, context in pending.items():
            result.setdefault(identity, []).append(context)
    return result, unreadable
