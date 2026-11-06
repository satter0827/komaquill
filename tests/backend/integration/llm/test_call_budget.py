"""Reject excess concurrent and expired inference reservations durably."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from komaquill.adapters.persistence.budgets import CallBudget
from komaquill.adapters.persistence.connection import Database
from komaquill.adapters.persistence.schema import execution_budgets
from komaquill.api import KomaquillError


def test_concurrent_reservations_cannot_exceed_the_allowance(tmp_path):
    database = Database(tmp_path / "calls.sqlite3", create=True)
    allowance = CallBudget(database, 1, 60, create=True)

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
    database = Database(tmp_path / "calls.sqlite3", create=True)
    allowance = CallBudget(database, 2, 60, create=True)
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with database.write() as connection:
        connection.execute(execution_budgets.update().values(expires_at=expired))
    recreated = CallBudget(database, 2, 60)
    with pytest.raises(KomaquillError, match="execution_budget_exceeded"):
        recreated.reserve()
    assert recreated.snapshot()["reserved_calls"] == 0
    assert recreated.snapshot()["expires_at"] == expired
    assert allowance.snapshot() == recreated.snapshot()


def test_new_workspace_cannot_recreate_a_shared_allowance_after_loss(tmp_path):
    from komaquill.api import Client

    ledger = tmp_path / "shared.sqlite3"
    with Client.initialize(tmp_path / "first") as first:
        with pytest.raises(KomaquillError, match="storage_failed"):
            first.limit_llm_calls(ledger, max_calls=1, max_seconds=60)
        assert not ledger.exists()
        first.limit_llm_calls(ledger, max_calls=1, max_seconds=60, create=True)
        first._execution.call_budget.reserve()
        before = first.llm_usage()
    with Client.initialize(tmp_path / "second") as second:
        second.limit_llm_calls(ledger, max_calls=1, max_seconds=60)
        assert second.llm_usage() == before
        with second._execution.call_budget.database.write() as connection:
            connection.execute(execution_budgets.delete())
    with (
        Client.initialize(tmp_path / "third") as third,
        pytest.raises(KomaquillError, match="storage_failed"),
    ):
        third.limit_llm_calls(ledger, max_calls=1, max_seconds=60, create=True)


def test_allowance_creation_flag_is_strict(tmp_path):
    from komaquill.api import Client

    with Client.initialize(tmp_path) as api, pytest.raises(KomaquillError, match="invalid_request"):
        api.limit_llm_calls(tmp_path / "calls.sqlite3", max_calls=1, max_seconds=60, create="yes")


@pytest.mark.parametrize("loss", ["file", "row"])
def test_attached_budget_loss_cannot_reset_the_allowance(tmp_path, loss):
    from komaquill.adapters.persistence.connection import database
    from komaquill.api import Client

    api = Client.initialize(tmp_path / "workspace")
    ledger = tmp_path / "budget.sqlite3"
    api.limit_llm_calls(ledger, max_calls=1, max_seconds=60, create=True)
    api._execution.call_budget.reserve()
    api.close()
    if loss == "file":
        ledger.unlink()
        database.cache_clear()
    else:
        with database(ledger).write() as connection:
            connection.execute(execution_budgets.delete())
    with pytest.raises(KomaquillError, match="storage_failed"):
        Client(api.workspace).llm_usage()
    with pytest.raises(KomaquillError, match="storage_failed"):
        api.limit_llm_calls(ledger, max_calls=1, max_seconds=60)
    if loss == "file":
        assert not ledger.exists()


@pytest.mark.parametrize("value", [{}, [], {"ledger": False, "max_calls": 1, "max_seconds": 60}])
def test_malformed_budget_binding_is_a_typed_configuration_failure(tmp_path, value):
    import json

    from komaquill.api import Client

    api = Client.initialize(tmp_path)
    (tmp_path / ".artifacts/state/llm-budget.json").write_text(json.dumps(value))
    with pytest.raises(KomaquillError, match="invalid_configuration"):
        Client(tmp_path).llm_usage()
    api.close()


@pytest.mark.parametrize("value", [0, True, 1.5, "1"])
def test_invalid_allowance_is_rejected_before_creating_a_ledger(tmp_path, value):
    from komaquill.api import Client

    api = Client.initialize(tmp_path / "workspace")
    ledger = tmp_path / "calls.sqlite3"
    with pytest.raises(KomaquillError, match="invalid_request"):
        api.limit_llm_calls(ledger, max_calls=value, max_seconds=60)
    assert not ledger.exists()
    assert api.llm_usage() == {}
    api.close()
