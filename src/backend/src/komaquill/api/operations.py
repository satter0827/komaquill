"""Expose operation checkpoints without exposing the persistence adapter."""

from uuid import UUID

from komaquill.application.conversations.service import Repository
from komaquill.domain.conversations.models import AcceptedInput, Operation


class OperationService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def get(self, identity: UUID) -> Operation:
        return self.repository.operation(identity)

    def input(self, identity: UUID) -> AcceptedInput:
        return self.repository.accepted_input(identity)

    def reconcile(self) -> None:
        self.repository.reconcile()
