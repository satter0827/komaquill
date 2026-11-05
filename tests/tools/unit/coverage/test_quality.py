"""Verify quality contracts within the declared test scope."""

import pytest

from tools.checks import coverage as check_coverage


def test_thresholds_are_independent_and_not_rounded():
    assert not check_coverage.check_totals(
        {"covered_lines": 90, "num_statements": 100, "covered_branches": 80, "num_branches": 100},
        90,
        80,
    )
    assert check_coverage.check_totals(
        {
            "covered_lines": 89995,
            "num_statements": 100000,
            "covered_branches": 79995,
            "num_branches": 100000,
        },
        90,
        80,
    ) == ["Statement", "Branch"]


@pytest.mark.parametrize(
    "totals",
    [
        {"covered_lines": 0, "num_statements": 0, "covered_branches": 0, "num_branches": 0},
        {"covered_lines": 101, "num_statements": 100, "covered_branches": 0, "num_branches": 1},
        {"covered_lines": "90", "num_statements": 100, "covered_branches": 1, "num_branches": 1},
    ],
)
def test_invalid_measurements_are_rejected(totals):
    with pytest.raises(ValueError):
        check_coverage.check_totals(totals, 90, 80)
