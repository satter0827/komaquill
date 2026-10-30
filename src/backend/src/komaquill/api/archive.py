"""Read retained works without modifying their original records or artifacts."""

from pathlib import Path
from uuid import UUID

from komaquill.adapters.archive import ArchiveRepository, load_archived_runs
from komaquill.adapters.locations import Locations
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.api._errors import api_operation
from komaquill.domain.errors import KomaquillError, OperationStage
from komaquill.domain.models import JsonObject


class ArchiveService:
    def __init__(self, project: RuntimeWorkspace) -> None:
        self.project = project
        self.repository = ArchiveRepository(project)

    def list(self) -> JsonObject:
        with api_operation(OperationStage.STORAGE):
            works = []
            unreadable = []
            for identity in self.repository.ids():
                try:
                    request = self.repository.request(identity)
                    works.append(
                        {
                            "id": str(identity),
                            "title": request.source_name,
                            "created_at": request.created_at.isoformat(),
                        }
                    )
                except KomaquillError:
                    unreadable.append(str(identity))
            invalid: list[UUID | str] = []
            images = load_archived_runs(self.project, unreadable=invalid)
            return {
                "works": works,
                "images": [value.model_dump(mode="json") for value in images],
                "unreadable_ids": [*unreadable, *map(str, invalid)],
            }

    def get(self, identity: UUID) -> JsonObject:
        with api_operation(OperationStage.STORAGE):
            if identity not in self.repository.ids():
                for run in load_archived_runs(self.project):
                    if run.run_id == identity:
                        return {"kind": "image", "run": run.model_dump(mode="json")}
                raise KomaquillError(code="request_not_found", stage="storage", retryable=False)
            plan = self.repository.read(identity)
            return {
                "plan": plan.model_dump(mode="json", exclude_unset=True),
                "planning": self.repository.planning_evidence(identity),
                "renders": [
                    value.model_dump(mode="json", exclude_unset=True)
                    for value in self.repository.renders(identity)
                ],
            }

    def artifact_path(self, key: str) -> Path:
        with api_operation(OperationStage.STORAGE):
            return Locations(self.project).artifact(key)

    def export(self, identity: UUID, destination: str | Path) -> Path:
        from komaquill.adapters.evaluations import write_export

        with api_operation(OperationStage.STORAGE):
            path = Path(destination)
            write_export(path, self.get(identity))
            return path
