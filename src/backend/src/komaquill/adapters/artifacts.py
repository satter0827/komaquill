"""Validate binary artifacts and publish them beneath the workspace data root."""

import hashlib
import io
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, UnidentifiedImageError

from komaquill.adapters.paths import WorkspacePaths, artifact_path
from komaquill.domain.errors import KomaquillError
from komaquill.domain.materials import InputArtifact
from komaquill.domain.models import ImageMetadata, ImageRef

if TYPE_CHECKING:
    from komaquill.adapters.persistence.artifacts import ArtifactRepository


class LocalArtifactStore:
    """Keep provider-neutral artifact keys separate from filesystem paths."""

    def __init__(self, project: WorkspacePaths) -> None:
        self.project = project
        self.root = project.data

    @property
    def repository(self) -> "ArtifactRepository":
        from komaquill.adapters.persistence.artifacts import ArtifactRepository
        from komaquill.adapters.persistence.connection import database

        return ArtifactRepository(database(self.root / "komaquill.sqlite3"))

    def path(self, key: str) -> Path:
        """Reject traversal and symlinks escaping the configured data root."""
        artifact_path(self.root, key)
        reference = self.repository.reference(key)
        if reference is None:
            raise FileNotFoundError(key)
        return self.repository.objects.verify(reference)

    def read(self, key: str) -> bytes:
        return self.path(key).read_bytes()

    def write(self, key: str, content: bytes) -> None:
        """Commit a blob atomically without changing identical existing content."""
        artifact_path(self.root, key)
        repository = self.repository
        try:
            reference = repository.objects.publish(content)
        except KomaquillError as error:
            retained = repository.reference(key)
            if error.code != "identity_mismatch" or retained is None:
                raise
            repository.objects.restore(retained, content)
            reference = retained
        repository.bind(key, reference)

    def cached_image(self, key: str, previous: ImageMetadata | None) -> bytes | None:
        """Reuse only a checkpoint whose saved checksum still matches."""
        if previous is None:
            return None
        try:
            content = self.read(key)
        except FileNotFoundError:
            return None
        except KomaquillError as error:
            if error.code == "identity_mismatch":
                return None
            raise
        if (
            len(content) != previous.size_bytes
            or hashlib.sha256(content).hexdigest() != previous.sha256
        ):
            return None
        return content

    def input_content(self, item: InputArtifact) -> bytes:
        """Verify retained bytes and mask semantics again at the point of use."""
        content = self.read(item.key)
        metadata = self.inspect_image(content, ImageRef(filename=item.name))
        if (metadata.sha256, metadata.size_bytes, metadata.width, metadata.height) != (
            item.sha256,
            item.size_bytes,
            item.width,
            item.height,
        ):
            raise KomaquillError(code="invalid_output", stage="preparation", retryable=False)
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                raise KomaquillError(code="unsupported_image", stage="preparation", retryable=False)
            if item.kind == "mask" and (image.mode not in {"1", "L"} or image.format != "PNG"):
                raise KomaquillError(code="invalid_request", stage="preparation", retryable=False)
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
