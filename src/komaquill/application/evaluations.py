"""Validate evaluation references without changing generation records."""

from contextlib import AbstractContextManager
from typing import Any, Protocol

from komaquill.application.ports import RunRepository
from komaquill.domain.errors import KomaquillError
from komaquill.domain.evaluations import Evaluation


class EvaluationRepository(Protocol):
    """Store immutable evaluations without requiring a database."""

    def lock(self) -> AbstractContextManager[Any]: ...
    def load(self) -> list[Evaluation]: ...
    def append(self, evaluation: Evaluation) -> None: ...


def register(
    runs: RunRepository, repository: EvaluationRepository, evaluations: list[Evaluation]
) -> None:
    """Validate the complete input before appending any new records."""
    with repository.lock():
        known = {item.evaluation_id: item for item in repository.load()}
        for item in evaluations:
            if item.supersedes and item.supersedes not in known:
                raise KomaquillError(code="invalid_evaluation", stage="evaluation")
            if item.supersedes == item.evaluation_id:
                raise KomaquillError(code="invalid_evaluation", stage="evaluation")
            fingerprints = {}
            for identity, image in (
                (item.run_id, item.image),
                (item.comparison_run_id, item.comparison_image),
            ):
                if identity is None:
                    continue
                try:
                    run = runs.read(identity)
                except FileNotFoundError:
                    raise KomaquillError(code="request_not_found", stage="evaluation") from None
                if run.pending or (image is not None and image not in run.images):
                    raise KomaquillError(code="invalid_evaluation", stage="evaluation")
                fingerprints[str(identity)] = run.workflow_sha256
            if item.workflow_sha256 and item.workflow_sha256 != fingerprints:
                raise KomaquillError(code="invalid_evaluation", stage="evaluation")
            item.workflow_sha256 = fingerprints
            previous = known.get(item.evaluation_id)
            if previous is not None and previous != item:
                raise KomaquillError(code="invalid_evaluation", stage="evaluation")
            known[item.evaluation_id] = item
        for item in evaluations:
            repository.append(item)
