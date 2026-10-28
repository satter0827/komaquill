"""Keep cumulative costs, reservations and ownership across verification restarts."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.live import verification_budget as budget


def create(tmp_path, **overrides):
    budget.path(tmp_path).parent.mkdir(parents=True)
    limits = dict(
        compute_unit_limit=9,
        compute_unit_reserve=0.5,
        output_limit=100,
        gpu_seconds=16200,
        session_seconds=5400,
        submission_seconds=4200,
    )
    limits.update(overrides)
    parent = {
        "saved_images": 17,
        "compute_budget": {
            "initial_balance": 129.62609261842726,
            "latest": {"remaining_compute_units": 128.8342229940643},
        },
    }
    return budget.initialize(tmp_path, parent, limits)


def test_carry_is_separate_from_new_baseline_and_cannot_reset(tmp_path):
    value = create(tmp_path)
    assert value.carried_compute_units == pytest.approx(0.79186962436296)
    assert value.runtime_compute_unit_limit == pytest.approx(8.20813037563704)
    with pytest.raises(ValueError, match="Resume"):
        budget.initialize(tmp_path, {}, {})


def test_allocations_exclude_local_work_and_count_unconfirmed_shutdown(tmp_path):
    create(tmp_path)
    now = datetime(2026, 10, 28, tzinfo=UTC)
    assert budget.begin(tmp_path, "first", now=now) == (4200, 5400)
    budget.checkpoint(tmp_path, "first", runtime_id="owned")
    with pytest.raises(ValueError, match="shutdown"):
        budget.begin(tmp_path, "second", now=now + timedelta(minutes=20))
    budget.checkpoint(tmp_path, "first", stopped_at=now + timedelta(minutes=30), released=True)
    later = now + timedelta(hours=10)
    assert budget.begin(tmp_path, "second", now=later) == (4200, 5400)
    assert budget.read(tmp_path).used_seconds(later) == 1800
    budget.checkpoint(tmp_path, "second", stopped_at=later + timedelta(minutes=2))
    assert budget.read(tmp_path).used_seconds(later + timedelta(minutes=3)) == 1980
    with pytest.raises(ValueError, match="cannot regress"):
        budget.checkpoint(tmp_path, "first")


def test_reservations_and_deadlines_survive_resume(tmp_path):
    create(tmp_path, output_limit=18)
    now = datetime.now(UTC)
    budget.begin(tmp_path, "first", now=now)
    assert budget.reserve(tmp_path, "uncertain", now=now)
    assert not budget.reserve(tmp_path, "uncertain", now=now)
    with pytest.raises(ValueError, match="count cannot change"):
        budget.reserve(tmp_path, "uncertain", 2, now=now)
    with pytest.raises(ValueError, match="exhausted"):
        budget.reserve(tmp_path, "another", now=now)
    with pytest.raises(ValueError, match="deadline"):
        budget.begin(tmp_path, "first", now=now + timedelta(minutes=70))


def test_last_allocation_uses_remaining_total_time(tmp_path):
    create(tmp_path, gpu_seconds=7200)
    now = datetime.now(UTC)
    budget.begin(tmp_path, "first", now=now)
    budget.checkpoint(tmp_path, "first", stopped_at=now + timedelta(seconds=5400), released=True)
    assert budget.begin(tmp_path, "second", now=now + timedelta(days=1)) == (600, 1800)
    with pytest.raises(ValueError, match="exhausted"):
        budget.reserve(tmp_path, "late", now=now + timedelta(days=1, minutes=10))


def test_opening_reconciliation_preserves_unknown_cost_without_claiming_attribution(tmp_path):
    from types import SimpleNamespace

    value = create(tmp_path)
    parent = {
        "saved_images": 17,
        "compute_budget": {
            "initial_balance": 129.62609261842726,
            "latest": {"remaining_compute_units": 128.8342229940643},
        },
    }
    # Use an account-bound parent for this independent scenario.
    from komaquill.domain.workflow import digest
    from komaquill.infrastructure.atomic import atomic_json

    parent["compute_budget"]["latest"]["account_sha256"] = "a" * 64
    value.parent_sha256 = digest(parent)
    atomic_json(budget.path(tmp_path), value.model_dump(mode="json"))
    usage = SimpleNamespace(
        assignments_count=0, account_sha256="a" * 64, remaining_compute_units=128.27
    )
    budget.reconcile_opening_usage(tmp_path, parent, usage)
    retained = budget.read(tmp_path)
    assert retained.unattributed_compute_units == pytest.approx(0.5642229940643)
    assert retained.opening_balance == 128.27
    with pytest.raises(ValueError):
        budget.reconcile_opening_usage(tmp_path, parent, usage)


@pytest.mark.parametrize(
    "record,valid,status,quality",
    [
        ({}, False, "not_submitted", "not_generated"),
        ({"status": "reserved"}, False, "acceptance_unknown", "not_generated"),
        ({"status": "failed", "run_id": "known"}, False, "generation_failed", "not_generated"),
        ({"status": "saved"}, False, "invalid_saved", "not_generated"),
        ({"status": "saved"}, True, "saved", "unreviewed"),
    ],
)
def test_quality_is_unknown_until_a_valid_saved_image_is_reviewed(record, valid, status, quality):
    from tests.live.klein_comparison import job_status

    assert job_status(record, valid_saved=valid, review=None) == {
        "generation_status": status,
        "quality_status": quality,
    }


def test_clock_rollback_does_not_refund_owned_time(tmp_path):
    create(tmp_path)
    now = datetime.now(UTC)
    budget.begin(tmp_path, "first", now=now)
    with pytest.raises(ValueError, match="clock"):
        budget.begin(tmp_path, "first", now=now - timedelta(seconds=1))
    budget.checkpoint(tmp_path, "first", stopped_at=now + timedelta(minutes=1), released=True)
    with pytest.raises(ValueError, match="clock"):
        budget.begin(tmp_path, "second", now=now)
    assert len(budget.read(tmp_path).allocations) == 1
