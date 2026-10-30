"""Expose operation checkpoints without exposing the persistence adapter."""

from uuid import UUID

from komaquill.conversations.models import Operation
from komaquill.conversations.service import Repository


class OperationService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def get(self, identity: UUID) -> Operation:
        return self.repository.operation(identity)

    def reconcile(self) -> None:
        self.repository.reconcile()
