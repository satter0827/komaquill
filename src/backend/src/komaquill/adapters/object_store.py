"""Publish immutable objects without buffering large imports or overwriting corruption."""

import hashlib
import io
import os
import shutil
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from filelock import FileLock

from komaquill.adapters.atomic import discard_temporary, replace_file
from komaquill.adapters.paths import artifact_path
from komaquill.domain.artifacts import ObjectRef
from komaquill.domain.errors import KomaquillError

COPY_BUFFER_BYTES = 1024 * 1024


class ObjectStore:
    def __init__(self, data: Path) -> None:
        self.root = data / "objects"
        self.locks = data.parent / "state/object-locks"

    def path(self, reference: ObjectRef) -> Path:
        return artifact_path(self.root, reference.sha256[:2] + "/" + reference.sha256)

    def verify(self, reference: ObjectRef) -> Path:
        path = self.path(reference)
        with path.open("rb") as source:
            if (
                os.fstat(source.fileno()).st_size != reference.size_bytes
                or hashlib.file_digest(source, "sha256").hexdigest() != reference.sha256
            ):
                raise KomaquillError(code="identity_mismatch", stage="storage", retryable=False)
        return path

    def read(self, reference: ObjectRef) -> bytes:
        content = self.path(reference).read_bytes()
        if (
            len(content) != reference.size_bytes
            or hashlib.sha256(content).hexdigest() != reference.sha256
        ):
            raise KomaquillError(code="identity_mismatch", stage="storage", retryable=False)
        return content

    def publish(self, content: bytes) -> ObjectRef:
        return self._publish(io.BytesIO(content))

    def restore(self, reference: ObjectRef, content: bytes) -> None:
        """Restore verified original bytes while preserving the damaged file as evidence."""
        if (
            len(content) != reference.size_bytes
            or hashlib.sha256(content).hexdigest() != reference.sha256
        ):
            raise KomaquillError(code="identity_mismatch", stage="storage")
        target = self.path(reference)
        lock = self.locks / (reference.sha256 + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock)):
            try:
                self.verify(reference)
                return
            except FileNotFoundError:
                pass
            except KomaquillError:
                preserved = self.root.parent / "quarantine" / uuid4().hex
                preserved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, preserved)
                with preserved.open("rb+") as source:
                    os.fsync(source.fileno())
            from komaquill.adapters.atomic import atomic_bytes

            atomic_bytes(target, content)

    def import_file(self, source: Path, *, expected_sha256: str | None = None) -> ObjectRef:
        with source.open("rb") as stream:
            return self._publish(stream, expected_sha256)

    def _publish(self, source: BinaryIO, expected_sha256: str | None = None) -> ObjectRef:
        temporary = artifact_path(self.root, ".tmp/" + uuid4().hex)
        temporary.parent.mkdir(parents=True, exist_ok=True)
        primary: BaseException | None = None
        try:
            digest, size = hashlib.sha256(), 0
            with temporary.open("xb") as destination:
                while chunk := source.read(COPY_BUFFER_BYTES):
                    destination.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            reference = ObjectRef(sha256=digest.hexdigest(), size_bytes=size)
            if expected_sha256 is not None and expected_sha256 != reference.sha256:
                raise KomaquillError(code="identity_mismatch", stage="storage", retryable=False)
            target = self.path(reference)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.locks.mkdir(parents=True, exist_ok=True)
            with FileLock(str(self.locks / (reference.sha256 + ".lock"))):
                if target.exists():
                    self.verify(reference)
                else:
                    replace_file(temporary, target)
            return reference
        except BaseException as error:
            primary = error
            raise
        finally:
            discard_temporary(temporary, primary)
