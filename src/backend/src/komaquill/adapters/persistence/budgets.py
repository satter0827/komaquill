"""Reserve calls before submission across processes and explicit restarts."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from komaquill.adapters.persistence.connection import Database
from komaquill.adapters.persistence.schema import execution_budgets
from komaquill.adapters.storage_errors import stored
from komaquill.domain.errors import KomaquillError


class CallBudget:
    @stored
    def __init__(self, database: Database, max_calls: int, max_seconds: int) -> None:
        if max_calls < 1 or max_seconds < 1:
            raise KomaquillError(code="invalid_request")
        self.database = database
        with database.transaction() as connection:
            previous = connection.execute(select(execution_budgets)).mappings().first()
            if previous is None:
                connection.execute(
                    execution_budgets.insert().values(
                        id="calls",
                        max_calls=max_calls,
                        max_seconds=max_seconds,
                        expires_at=(datetime.now(UTC) + timedelta(seconds=max_seconds)).isoformat(),
                    )
                )
                connection.commit()
            elif previous["max_calls"] != max_calls or previous["max_seconds"] != max_seconds:
                raise KomaquillError(code="operation_conflict")

    @stored
    def reserve(self) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(select(execution_budgets)).mappings().one()
            if row["reserved_calls"] >= row["max_calls"] or datetime.now(
                UTC
            ) >= datetime.fromisoformat(row["expires_at"]):
                raise KomaquillError(
                    code="execution_budget_exceeded", stage="submission", retryable=False
                )
            connection.execute(
                execution_budgets.update().values(reserved_calls=row["reserved_calls"] + 1)
            )
            connection.commit()

    @stored
    def snapshot(self) -> dict[str, object]:
        with self.database.engine.connect() as connection:
            return dict(connection.execute(select(execution_budgets)).mappings().one())
