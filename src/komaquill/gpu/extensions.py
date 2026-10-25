"""Install only reviewed node packages required by enabled presets."""

import subprocess
import tomllib
from importlib.resources import files
from pathlib import Path

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import KomaquillError


def prepare_extensions(comfy: Path, settings: RuntimeSettings) -> None:
    """Pin reviewed packages without running installers or dependency updates."""
    catalog = tomllib.loads(
        files("komaquill").joinpath("resources/runtime/extensions.toml").read_text()
    )
    needed = dict.fromkeys(
        extension for name in settings.profiles for extension in settings.presets[name].extensions
    )
    for name in needed:
        spec = catalog[name]
        target = comfy / "custom_nodes" / f"komaquill-{name}"
        if not target.exists():
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--filter=blob:none",
                    "--no-checkout",
                    spec["repository"],
                    str(target),
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(target), "checkout", "--detach", spec["revision"]],
                check=True,
                capture_output=True,
            )
        actual = subprocess.check_output(
            ["git", "-C", str(target), "rev-parse", "HEAD"], text=True
        ).strip()
        if actual != spec["revision"]:
            raise KomaquillError(code="identity_mismatch", stage="preparation", retryable=False)
