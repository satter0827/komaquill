"""Build an immutable worker payload from installed package resources."""

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from komaquill.adapters.atomic import atomic_bytes, atomic_json
from komaquill.adapters.provenance import code_sha256, gpu_contents, source_revision
from komaquill.adapters.workspace import RuntimeWorkspace


@dataclass(frozen=True)
class Payload:
    """Identify a worker archive prepared before allocation."""

    path: Path
    sha256: str
    code_sha256: str
    source_revision: str | None


def create_payload(project: RuntimeWorkspace) -> Payload:
    """Archive only executable resources and non-secret validated settings."""
    settings = project.settings()
    contents = list(gpu_contents())
    fingerprint = code_sha256(contents)
    revision = source_revision()
    entries = dict(contents)
    entries["komaquill.toml"] = (
        "\n".join(
            f"{key} = {json.dumps(value)}"
            for key, value in settings.model_dump().items()
            if key not in {"presets", "assets"} and value is not None
        )
        + "\n"
    ).encode()
    entries["manifest.json"] = json.dumps(
        {
            "app_version": version("komaquill"),
            "settings": settings.model_dump(mode="json"),
            "source_revision": revision,
            "code_sha256": fingerprint,
        },
        sort_keys=True,
    ).encode()
    path = project.runtime / "deployment/payload.tar"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w") as archive:
        for name, entry in sorted(entries.items()):
            member = tarfile.TarInfo(name)
            member.size = len(entry)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(entry))
    atomic_bytes(path, content.getvalue())
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    atomic_json(
        path.with_suffix(".json"),
        {
            "sha256": digest,
            "code_sha256": fingerprint,
            "source_revision": revision,
            "app_version": version("komaquill"),
        },
    )
    return Payload(path, digest, fingerprint, revision)
