"""Open native ComfyUI views without replacing its workflow editor."""

import webbrowser
from collections.abc import Callable
from typing import Literal
from urllib.parse import urlencode

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import Session
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.workflows import prepare, select
from komaquill.messages import message


def open_view(
    project: Project,
    session: Session,
    view: Literal["app", "graph"] = "app",
    profile_name: str | None = None,
    echo: Callable[[str], None] = print,
    *,
    strict: bool = True,
) -> bool:
    """Open a prepared profile, retaining the monitor if automatic launch fails."""
    project = project.configured(session.settings)
    name, profile = select(project.settings(), profile_name)
    if session.backend == "colab":
        url = (
            session.endpoint
            + "/?"
            + urlencode({"template": f"{name}_{view}", "source": "komaquill"})
        )
    else:
        _, workflow = prepare(
            profile.sample_prompt,
            None,
            1,
            name,
            settings=project.settings(),
            profile_name=name,
        )
        workflow["extra"]["linearMode"] = view == "app"
        path = project.runtime / "workflows" / f"{name}_{view}.json"
        atomic_json(path, workflow)
        echo(message("text.load_this_workflow_in_comfyui_path", path=path))
        url = session.endpoint + "/"
    try:
        opened = webbrowser.open(url)
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        if strict:
            raise KomaquillError(
                display_key="text.cannot_open_the_browser_open_this_url_url",
                details={"url": url},
                code="browser_failed",
                retryable=False,
            )
        echo(message("text.cannot_open_the_browser_open_this_url_url", url=url))
        return False
    echo(message("text.browser_opened_keep_start_running_to_save_results"))
    return True
