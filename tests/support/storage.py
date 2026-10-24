"""Build isolated legacy layouts from real generation and story records."""

from komaquill.application.stories.rendering import create_render, render_story
from komaquill.infrastructure.atomic import atomic_bytes
from komaquill.infrastructure.configuration import initialize
from komaquill.infrastructure.stories.storage import FileStoryRepository
from tests.support.stories import planned


def legacy_story(project, session, client, count=2):
    # Construct source records separately; directory renames are not part of migration.
    builder = initialize(project.root / "fixture-source").configured(project.settings())
    repository, plan = planned(builder, count)
    active = session.model_copy(update={"settings": builder.settings()})
    render = create_render(repository, builder, active, plan.request.story_id, "pony", seed=7)
    render_story(repository, builder, client, active, render.render_id)
    render = repository.render(render.render_id)
    protected = {}
    for run in builder.repository.load():
        target = project.runs / str(run.run_id)
        source = builder.artifacts_store.path(f"runs/{run.run_id}/run.json").parent
        for file in source.rglob("*"):
            if file.is_file():
                relative = file.relative_to(source)
                content = file.read_bytes()
                atomic_bytes(target / relative, content)
                protected[(str(run.run_id), relative.as_posix())] = content
        for name in run.images:
            content = builder.artifacts_store.read(f"runs/{run.run_id}/{name}")
            atomic_bytes(target / name, content)
            protected[(str(run.run_id), name)] = content
    source = repository.folder(plan.request.story_id)
    target = FileStoryRepository(project).root / str(plan.request.story_id)
    for file in source.rglob("*"):
        if file.is_file() and not file.relative_to(source).as_posix().startswith("renders/"):
            atomic_bytes(target / file.relative_to(source), file.read_bytes())
    for file in source.glob("renders/*/render.json"):
        atomic_bytes(target / file.relative_to(source), file.read_bytes())
    return plan, render, protected
