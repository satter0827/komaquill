"""List works independently of filesystem layout, viewers and model connections."""

from komaquill.application.ports import Workspace
from komaquill.application.stories.ports import StoryRepository
from komaquill.application.stories.rendering import story_view
from komaquill.domain.errors import KomaquillError
from komaquill.domain.story_views import StoryListing, StorySummary


def list_stories(repository: StoryRepository, workspace: Workspace) -> StoryListing:
    rows = []
    unreadable = []
    for identity in repository.ids():
        try:
            request = repository.request(identity)
            try:
                plan = repository.read(identity)
            except FileNotFoundError:
                plan = None
            view = story_view(repository, workspace, identity) if plan else None
            rows.append(
                StorySummary(
                    story_id=identity,
                    title=plan.title if plan else request.source_name,
                    created_at=request.created_at,
                    requested_images=request.image_count,
                    image_count=len(plan.scenes) if plan else None,
                    render_id=view["render_id"] if view else None,
                    saved_images=view["counts"].get("saved", 0) if view else 0,
                    state=(
                        "unplanned"
                        if view is None
                        else "planned"
                        if view["render_id"] is None
                        else "complete"
                        if view["complete"]
                        else "incomplete"
                    ),
                    purpose=request.purpose,
                    case_id=request.case_id,
                )
            )
        except (OSError, ValueError, KomaquillError):
            unreadable.append(identity)
    rows.sort(key=lambda row: (row.created_at, str(row.story_id)), reverse=True)
    return StoryListing(stories=rows, unreadable_ids=unreadable)
