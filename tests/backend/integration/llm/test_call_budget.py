"""Reject excess concurrent and expired inference reservations durably."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from komaquill.adapters.persistence.budgets import CallBudget
from komaquill.adapters.persistence.connection import Database
from komaquill.adapters.persistence.schema import execution_budgets
from komaquill.api import KomaquillError


def test_concurrent_reservations_cannot_exceed_the_allowance(tmp_path):
    database = Database(tmp_path / "calls.sqlite3")
    allowance = CallBudget(database, 1, 60)

    def reserve(_):
        try:
            allowance.reserve()
            return True
        except KomaquillError as error:
            assert error.code == "execution_budget_exceeded"
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(4))) == 1
    assert CallBudget(database, 1, 60).snapshot()["reserved_calls"] == 1


def test_restart_cannot_refresh_an_expired_deadline(tmp_path):
    database = Database(tmp_path / "calls.sqlite3")
    allowance = CallBudget(database, 2, 60)
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with database.write() as connection:
        connection.execute(execution_budgets.update().values(expires_at=expired))
    recreated = CallBudget(database, 2, 60)
    with pytest.raises(KomaquillError, match="execution_budget_exceeded"):
        recreated.reserve()
    assert recreated.snapshot()["reserved_calls"] == 0
    assert recreated.snapshot()["expires_at"] == expired
    assert allowance.snapshot() == recreated.snapshot()
