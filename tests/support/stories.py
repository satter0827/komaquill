"""Create coherent story fixtures through the public planning contracts."""

import hashlib
from importlib.resources import files

from komaquill.application.stories.planning import plan_story
from komaquill.domain.stories import StoryInput
from komaquill.infrastructure.stories.configuration import settings
from komaquill.infrastructure.stories.llm import planner
from komaquill.infrastructure.stories.storage import FileStoryRepository


def source():
    return (
        files("komaquill")
        .joinpath("resources/stories/examples/rainy-cat/source.md")
        .read_text(encoding="utf-8")
    )


def request(count=4, text=None):
    text = source() if text is None else text
    return StoryInput(
        source_name="story.md",
        source_text=text,
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        image_count=count,
    )


def planned(project, count=4):
    options = settings(project, images=count, llm_profile="fake")
    repository = FileStoryRepository(project)
    result = plan_story(
        repository,
        planner(project, options, allow_real_llm=False),
        request(count),
        max_images=options.max_images,
        repair_attempts=options.repair_attempts,
    )
    return repository, result
