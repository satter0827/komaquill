"""Validate distribution versions and exclude generated or secret files."""

import argparse
import os
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from importlib.metadata import version
from pathlib import Path

from komaquill.messages import message


def main(dist_dir: Path | None = None) -> None:
    """Compare the wheel, sdist, declaration and release tag versions."""
    root = Path(__file__).resolve().parents[1]
    expected = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    if expected != version("komaquill"):
        raise SystemExit(
            message("quality.synchronize_the_environment_to_match_the_declared_version")
        )
    if (
        os.environ.get("GITHUB_REF_TYPE") == "tag"
        and os.environ.get("GITHUB_REF_NAME") != f"v{expected}"
    ):
        raise SystemExit(message("quality.the_release_tag_does_not_match_the_declared"))
    dist = dist_dir or root / ".artifacts/dist"
    wheel_path = dist / f"komaquill-{expected}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        metadata_name = next((name for name in names if name.endswith(".dist-info/METADATA")), None)
        if metadata_name is None:
            raise SystemExit(message("quality.the_wheel_is_missing_metadata"))
        metadata = Parser().parsestr(wheel.read(metadata_name).decode())
        if metadata["Version"] != expected:
            raise SystemExit(message("quality.the_wheel_version_does_not_match_the_declaration"))
        from komaquill.infrastructure.provenance import RESOURCE_NAMES

        required = set(RESOURCE_NAMES)
        if "komaquill/gpu/bootstrap.py" not in names or not all(
            f"komaquill/resources/{name}" in names for name in required
        ):
            raise SystemExit(
                message("quality.required_workflow_configuration_or_startup_resources_are_missing")
            )
    with tarfile.open(dist / f"komaquill-{expected}.tar.gz") as archive:
        names += archive.getnames()
        metadata_path = f"komaquill-{expected}/PKG-INFO"
        if metadata_path not in archive.getnames():
            raise SystemExit(message("quality.the_source_distribution_is_missing_pkg_info"))
        member = archive.extractfile(metadata_path)
        if member is None or Parser().parsestr(member.read().decode())["Version"] != expected:
            raise SystemExit(message("quality.the_source_distribution_version_does_not_match_the"))
    if any({".artifacts", "artifacts", ".env"}.intersection(Path(name).parts) for name in names):
        raise SystemExit(
            message("quality.the_distribution_includes_generated_data_or_credential_files")
        )
    if any(Path(name).parts[-2:] == (".vscode", "settings.json") for name in names):
        raise SystemExit(
            message("quality.the_distribution_includes_generated_data_or_credential_files")
        )
    print(message("quality.distribution_versions_and_contents_verified"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=message("quality.inspect_distributions"))
    parser.add_argument("--dist-dir", type=Path)
    main(parser.parse_args().dist_dir)
