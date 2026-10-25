"""Import image and mask bytes into the existing immutable material store."""

import hashlib
import io
from pathlib import Path
from typing import Literal

from filelock import FileLock
from PIL import Image

from komaquill.domain.errors import KomaquillError
from komaquill.domain.materials import InputArtifact, Material
from komaquill.domain.models import ImageRef
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.materials import workspace_identity


def import_file(project: Project, path: Path, kind: Literal["image", "mask"]) -> InputArtifact:
    """Preserve original bytes and reject masks with ambiguous color semantics."""
    content = path.read_bytes()
    metadata = project.artifacts_store.inspect_image(content, ImageRef(filename=path.name))
    with Image.open(io.BytesIO(content)) as image:
        if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
            raise KomaquillError(
                code="unsupported_image", stage="image_validation", retryable=False
            )
        if kind == "mask" and (image.format != "PNG" or image.mode not in {"1", "L"}):
            raise KomaquillError(code="invalid_request", stage="image_validation", retryable=False)
    item = InputArtifact(
        kind=kind,
        key=f"inputs/{metadata.sha256}/content",
        sha256=metadata.sha256,
        name=path.name,
        size_bytes=metadata.size_bytes,
        width=metadata.width,
        height=metadata.height,
    )
    project.state.mkdir(parents=True, exist_ok=True)
    with FileLock(str(project.state / ".materials.lock")):
        workspace_id = workspace_identity(project)
        project.artifacts_store.write(item.key, content)
        record = Material(
            workspace_id=workspace_id,
            kind="inputs",
            name=item.name,
            reference=item.reference,
            sha256=item.sha256,
            size_bytes=item.size_bytes,
            state="saved",
        )
        identity = hashlib.sha256(f"{kind}:{item.sha256}:{item.name}".encode()).hexdigest()
        target = project.data / "inputs/records" / f"local-{identity}.json"
        if not target.exists():
            atomic_json(target, record.model_dump(mode="json"))
    return item
