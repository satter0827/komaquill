"""Exercise the public CLI after isolated wheel installation."""

import os
from pathlib import Path

import pytest

from tests.support.acceptance import run_acceptance


@pytest.mark.skipif(
    not os.environ.get("KOMAQUILL_WHEEL"), reason="Set KOMAQUILL_WHEEL after building"
)
def test_installed_wheel_generates_and_prepares_payload_without_git():
    directory = Path(os.environ["KOMAQUILL_WHEEL"]).resolve()
    run_acceptance(next(directory.glob("komaquill-*-py3-none-any.whl")))
