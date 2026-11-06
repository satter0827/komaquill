"""Persist evaluation records independently of export and runtime control."""

from contextlib import AbstractContextManager

from filelock import FileLock

from komaquill.adapters.paths import WorkspacePaths
from komaquill.domain.errors import KomaquillError
from komaquill.domain.evaluations import Evaluation


class EvaluationRepository:
    """Append records under a single lock without changing original evaluations."""

    def __init__(self, project: WorkspacePaths) -> None:
        self.project = project

    def lock(self) -> AbstractContextManager[object]:
        self.project.state.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.project.state / ".evaluations.lock"))

    def load(self, unreadable: list[str] | None = None) -> list[Evaluation]:
        from sqlalchemy import select

        from komaquill.adapters.persistence.connection import database
        from komaquill.adapters.persistence.schema import evaluations

        storage = database(self.project.data / "komaquill.sqlite3")
        with storage.engine.connect() as connection:
            values = connection.execute(select(evaluations)).mappings().all()
        rows = []
        for value in values:
            try:
                evaluation = Evaluation.model_validate_json(value["payload"])
                if str(evaluation.evaluation_id) != value["id"]:
                    raise ValueError("Evaluation identity mismatch.")
                rows.append(evaluation)
            except ValueError:
                if unreadable is None:
                    raise KomaquillError(code="storage_failed", stage="evaluation") from None
                unreadable.append(value["id"])
        return sorted(rows, key=lambda row: (row.created_at, str(row.evaluation_id)))

    def append(self, evaluation: Evaluation) -> None:
        from sqlalchemy import select

        from komaquill.adapters.persistence.connection import database
        from komaquill.adapters.persistence.schema import evaluations

        storage = database(self.project.data / "komaquill.sqlite3")
        with storage.transaction() as connection:
            previous = connection.execute(
                select(evaluations.c.payload).where(
                    evaluations.c.id == str(evaluation.evaluation_id)
                )
            ).scalar_one_or_none()
            if previous is not None:
                if Evaluation.model_validate_json(previous) != evaluation:
                    raise KomaquillError(code="invalid_evaluation", stage="evaluation")
                return
            connection.execute(
                evaluations.insert().values(
                    id=str(evaluation.evaluation_id), payload=evaluation.model_dump_json()
                )
            )
            connection.commit()
