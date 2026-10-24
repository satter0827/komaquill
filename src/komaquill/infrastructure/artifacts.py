"""Validate binary artifacts and publish them beneath the workspace data root."""

import hashlib
import io
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import ImageMetadata, ImageRef
from komaquill.infrastructure.atomic import atomic_bytes
from komaquill.infrastructure.locations import Locations
from komaquill.infrastructure.paths import WorkspacePaths


def file_digest(path: Path) -> str:
    """Verify file contents with bounded memory."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class LocalArtifactStore:
    """Keep provider-neutral artifact keys separate from filesystem paths."""

    def __init__(self, project: WorkspacePaths) -> None:
        self.project = project
        self.root = project.data

    def path(self, key: str) -> Path:
        """Reject traversal and symlinks escaping the configured data root."""
        return Locations(self.project).artifact(key)

    def read(self, key: str) -> bytes:
        return self.path(key).read_bytes()

    def write(self, key: str, content: bytes) -> None:
        """Commit a blob atomically without changing identical existing content."""
        parts = PurePosixPath(key).parts
        if len(parts) >= 3 and parts[0] == "runs":
            from uuid import UUID

            Locations(self.project).register_run(UUID(parts[1]))
        target = self.path(key)
        if not target.is_file() or target.read_bytes() != content:
            atomic_bytes(target, content)

    def cached_image(self, key: str, previous: ImageMetadata | None) -> bytes | None:
        """Reuse only a checkpoint whose saved checksum still matches."""
        target = self.path(key)
        if previous is None or not target.is_file():
            return None
        content = target.read_bytes()
        if (
            len(content) != previous.size_bytes
            or hashlib.sha256(content).hexdigest() != previous.sha256
        ):
            return None
        return content

    def inspect_image(self, content: bytes, source: ImageRef) -> ImageMetadata:
        """Validate image bytes before taking the metadata update lock."""
        try:
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                image.verify()
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
            raise KomaquillError(code="image_invalid", stage="image_validation") from None
        return ImageMetadata(
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            width=width,
            height=height,
            source=source,
        )
