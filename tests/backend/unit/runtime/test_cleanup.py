"""Verify runtime contracts within the declared test scope."""

import pytest
from komaquill.application.runtime.cleanup import exclusion


@pytest.mark.parametrize(
    "category,name,protected,known,busy,reason",
    [
        ("dist", "a" * 32, set(), True, False, None),
        ("dist", "a" * 32, {"a" * 32}, True, False, "retained"),
        ("dist", "unknown", set(), False, False, "unknown"),
        ("dist", "b" * 32, set(), False, False, "unknown"),
        ("cache", "mypy", set(), False, True, "in_use"),
        ("cache", "uv", set(), False, True, None),
        ("cache", "unknown", set(), False, False, "unknown"),
        ("tmp", "pytest-1", set(), False, False, None),
        ("tmp", "pytest-1", set(), False, True, "in_use"),
        ("tmp", "editor.log", set(), False, False, "unknown"),
        ("data", "runs", set(), False, False, "protected"),
    ],
)
def test_retention_policy(category, name, protected, known, busy, reason):
    assert exclusion(category, name, protected, known, busy) == reason
