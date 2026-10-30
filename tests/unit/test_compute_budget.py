"""Verify durable compute limits and safe handling of uncertain observations."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from komaquill.adapters import compute_budget
from komaquill.adapters.colab import Colab
from komaquill.domain.errors import KomaquillError
from komaquill.domain.usage import AccountUsage, ComputeBudget


def observation(balance=100, count=0, rate=0, age=0, account="a" * 64):
    return AccountUsage(
        remaining_compute_units=balance,
        assignments_count=count,
        consumption_rate_hourly=rate,
        observed_at=datetime.now(UTC) - timedelta(seconds=age),
        account_sha256=account,
    )


@pytest.fixture
def budget(project, monkeypatch):
    project.state.mkdir(parents=True, exist_ok=True)
    project = project.configured(project.settings().model_copy(update={"compute_unit_limit": 20.0}))
    state = SimpleNamespace(usage=observation(), receipt=None, queries=0)

    def usage(_):
        state.queries += 1
        return state.usage

    monkeypatch.setattr(Colab, "usage", usage)
    monkeypatch.setattr(Colab, "receipt", lambda _: state.receipt)
    monkeypatch.setattr(Colab, "refresh", lambda *args: True)
    monkeypatch.setattr(compute_budget, "OBSERVATION_INTERVAL_SECONDS", 0)
    return project, state


def test_budget_is_preserved_across_sessions_and_stops_at_reserve(budget):
    project, state = budget
    compute_budget.enforce(project)
    state.receipt = SimpleNamespace(
        endpoint_id="owned", account_sha256="a" * 64, settings=None, runtime_id=uuid4()
    )
    state.usage = observation(balance=85, count=1, rate=10)
    compute_budget.enforce(project)
    state.receipt = None
    state.usage = observation(balance=84)
    compute_budget.enforce(project)
    state.receipt = SimpleNamespace(
        endpoint_id="next", account_sha256="a" * 64, settings=None, runtime_id=uuid4()
    )
    state.usage = observation(balance=82, count=1, rate=10)
    with pytest.raises(KomaquillError) as error:
        compute_budget.enforce(project)
    assert error.value.code == "stopping"
    saved = ComputeBudget.model_validate_json((project.state / "compute-budget.json").read_bytes())
    assert saved.initial_balance == 100 and saved.consumed == 18


@pytest.mark.parametrize(
    "fault", ["account", "assignments", "increase", "stale", "rate", "balance"]
)
def test_uncertain_or_unaffordable_observation_blocks_submission(budget, fault):
    project, state = budget
    compute_budget.enforce(project)
    state.usage = {
        "account": observation(account="b" * 64),
        "assignments": observation(count=1),
        "increase": observation(balance=101),
        "stale": observation(age=120),
        "rate": observation(rate=1000),
        "balance": observation(balance=0),
    }[fault]
    with pytest.raises(KomaquillError):
        compute_budget.enforce(project)


def test_recent_observation_is_reused_without_resetting_the_baseline(budget, monkeypatch):
    project, state = budget
    compute_budget.enforce(project)
    monkeypatch.setattr(compute_budget, "OBSERVATION_INTERVAL_SECONDS", 20)
    compute_budget.enforce(project)
    assert state.queries == 1


def test_query_failure_preserves_previous_measurement(budget, monkeypatch):
    project, _ = budget
    compute_budget.enforce(project)
    path = project.state / "compute-budget.json"
    before = path.read_bytes()

    def fail(_):
        raise KomaquillError(code="colab_failed")

    monkeypatch.setattr(Colab, "usage", fail)
    with pytest.raises(KomaquillError):
        compute_budget.enforce(project)
    assert path.read_bytes() == before


def test_disabled_budget_never_queries_provider(project, monkeypatch):
    monkeypatch.setattr(Colab, "usage", lambda _: pytest.fail("Unexpected provider call"))
    compute_budget.enforce(project)


def test_startup_observes_spending_while_the_owned_allocation_is_pending(budget):
    project, state = budget
    compute_budget.enforce(project)
    state.receipt = SimpleNamespace(
        endpoint_id=None, account_sha256="a" * 64, settings=None, runtime_id=uuid4()
    )
    state.usage = observation(balance=99, count=1, rate=10)
    compute_budget.enforce(project, allocation_pending=True)
    with pytest.raises(KomaquillError):
        compute_budget.enforce(project)
    state.usage = observation(balance=82, count=1, rate=10)
    with pytest.raises(KomaquillError):
        compute_budget.enforce(project, allocation_pending=True)
