"""Retain useful source positions without exposing private paths or exception bodies."""

import pytest
from komaquill.api import describe_failure


@pytest.mark.parametrize(
    "filename,expected",
    [
        (
            "C:/work/komaquill/src/backend/src/komaquill/adapters/storage.py",
            "komaquill/adapters/storage.py",
        ),
        ("C:/work/komaquill/scripts/acceptance/run.py", "scripts/acceptance/run.py"),
        ("C:/work/komaquill/.artifacts/runtime/env/Lib/site-packages/vendor/client.py", None),
    ],
)
def test_diagnostic_locations_choose_the_owning_source_boundary(filename, expected):
    try:
        exec(compile("raise OSError('private response body')", filename, "exec"), {})
    except OSError as error:
        info = describe_failure(error, "storage")
    assert "private response body" not in info.model_dump_json()
    locations = [item.path for item in info.locations]
    assert locations == ([expected] if expected else [])
