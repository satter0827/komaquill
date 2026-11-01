"""Reconcile saved creation work without model, GPU-control or presentation dependencies."""

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import Run
from komaquill.library.models import CreationJob


class CreationStore(Protocol):
    def job(self, identity: UUID) -> tuple[CreationJob, bool]: ...
    def job_ids(self) -> list[UUID]: ...
    def save_job(self, value: CreationJob) -> CreationJob: ...


def reconcile_job(
    store: CreationStore, read_image: Callable[[UUID], Run], identity: UUID
) -> CreationJob:
    job, alive = store.job(identity)
    observed = job
    if job.state in {"accepted", "running", "submitted"} and not job.request.kind.endswith(
        "_draft"
    ):
        try:
            run = read_image(job.id)
        except KomaquillError as error:
            if error.code != "request_not_found":
                raise
        else:
            if run.state == "saved" and run.images:
                job = job.model_copy(
                    update={"state": "complete", "image_id": run.run_id, "error": None}
                )
            elif run.state in {"failed", "lost"}:
                job = job.model_copy(update={"state": "failed", "error": "generation_failed"})
            elif run.state != "prepared":
                job = job.model_copy(
                    update={
                        "state": "submitted",
                        "image_id": run.run_id,
                        "error": "submission_unknown" if run.state == "unknown" else None,
                    }
                )
    if not alive and job.state in {"accepted", "running"}:
        job = job.model_copy(update={"state": "interrupted", "error": "submission_unknown"})
    return store.save_job(job) if job != observed else store.job(identity)[0]


def reconcile_jobs(store: CreationStore, read_image: Callable[[UUID], Run]) -> list[UUID]:
    errors = []
    for identity in store.job_ids():
        try:
            reconcile_job(store, read_image, identity)
        except KomaquillError:
            errors.append(identity)
    return errors
