"""Compose image, session and evaluation use cases for Python and terminal callers."""

from uuid import UUID

from komaquill.adapters.archive_associations import story_associations
from komaquill.adapters.evaluations import EvaluationRepository, export_evidence, select
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.api._errors import api_operation
from komaquill.application.evaluations import register
from komaquill.domain.errors import OperationStage
from komaquill.domain.evaluations import Evaluation
from komaquill.domain.models import JsonObject


class EvaluationService:
    def __init__(self, project: RuntimeWorkspace) -> None:
        self._project = project

    def add(self, records: list[Evaluation]) -> list[Evaluation]:
        with api_operation(OperationStage.EVALUATION):
            register(self._project.repository, EvaluationRepository(self._project), records)
            return records

    def list(self, run_id: UUID | None = None) -> list[Evaluation]:
        with api_operation(OperationStage.EVALUATION):
            return select(self._project, run_id)

    def export(self, run_id: UUID | None = None) -> JsonObject:
        with api_operation(OperationStage.EVALUATION):
            payload = export_evidence(self._project, run_id)
            links, unreadable = story_associations(self._project)
            payload["unreadable_story_renders"] = unreadable
            for row in payload["runs"]:
                row["story_context"] = links.get(row["run"]["run_id"], [])
            return payload
