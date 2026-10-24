"""Build stopped legacy workspaces from real generation and story records."""

from komaquill.application.stories.rendering import create_render, render_story
from komaquill.infrastructure.locations import Locations
from tests.support.stories import planned


def legacy_story(project, session, client, count=2):
    repository, plan = planned(project, count)
    active = session.model_copy(update={"settings": project.settings()})
    render = create_render(repository, project, active, plan.request.story_id, "pony", seed=7)
    render_story(repository, project, client, active, render.render_id)
    locations = Locations(project)
    protected = {}
    for run in project.repository.load():
        old = project.runs / str(run.run_id)
        old.parent.mkdir(parents=True, exist_ok=True)
        source = locations.run(run.run_id)
        assert source.resolve().is_relative_to(project.data.resolve())
        assert old.resolve().is_relative_to(project.data.resolve())
        source.replace(old)
        for name in run.images:
            physical = locations.artifact(f"runs/{run.run_id}/{name}")
            target = old / name
            target.parent.mkdir(parents=True, exist_ok=True)
            physical.replace(target)
        for path in old.rglob("*"):
            if path.is_file():
                protected[(str(run.run_id), path.relative_to(old).as_posix())] = path.read_bytes()
    source = repository.folder(plan.request.story_id)
    target = repository.root / str(plan.request.story_id)
    assert source.resolve().is_relative_to(project.data.resolve())
    assert target.resolve().is_relative_to(project.data.resolve())
    source.replace(target)
    locations.index_file.unlink()
    return plan, render, protected
