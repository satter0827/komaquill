"""Connect local creation completion to collection without constructing an API client."""

import json
from pathlib import Path
from uuid import UUID

from filelock import FileLock


def reconcile_creations(workspace: Path) -> None:
    """Finish saved image references without making new remote requests."""
    if not (workspace / ".artifacts/data/komaquill.sqlite3").is_file():
        return
    from komaquill.adapters.database import database
    from komaquill.adapters.diagnostics import record_event
    from komaquill.adapters.library_repository import LibraryRepository
    from komaquill.adapters.storage_errors import stored
    from komaquill.adapters.workspace import RuntimeWorkspace
    from komaquill.domain.errors import KomaquillError, failure_info
    from komaquill.domain.models import Run
    from komaquill.library.operations import reconcile_jobs

    project = RuntimeWorkspace(workspace)

    @stored
    def read_image(identity: UUID) -> Run:
        try:
            return project.repository.read(identity)
        except FileNotFoundError:
            raise KomaquillError(code="request_not_found", retryable=False) from None

    failures = {}
    try:
        store = LibraryRepository(database(workspace / ".artifacts/data/komaquill.sqlite3"))
        failures = {
            str(identity): "storage_failed" for identity in reconcile_jobs(store, read_image)
        }
    except KomaquillError as error:
        failures = {"workspace": error.code.value}
    path = project.state / "creation-reconciliation.json"
    try:
        from komaquill.adapters.atomic import atomic_json

        with FileLock(str(path) + ".lock"):
            previous = json.loads(path.read_bytes()).get("failures", {}) if path.exists() else {}
            if previous == failures:
                return
            recorded = True
            for identity in set(previous) | set(failures):
                if previous.get(identity) == failures.get(identity):
                    continue
                code = failures.get(identity)
                recorded &= record_event(
                    project,
                    "creation_reconciliation",
                    "failed" if code else "restored",
                    operation_id=UUID(identity) if identity != "workspace" else None,
                    failure=failure_info(KomaquillError(code=code), "storage") if code else None,
                )
            if recorded:
                atomic_json(path, {"format_version": 1, "failures": failures})
    except (OSError, ValueError, TypeError, AttributeError):
        record_event(
            project,
            "creation_reconciliation",
            "failed",
            failure=failure_info(KomaquillError(code="storage_failed"), "storage"),
        )
