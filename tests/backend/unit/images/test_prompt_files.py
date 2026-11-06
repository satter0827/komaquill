"""Verify images contracts within the declared test scope."""

from pathlib import Path

import pytest
from komaquill.domain.errors import KomaquillError
from komaquill.interfaces.cli.image_inputs import image_input


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": "a", "prompt_file": Path("positive.txt")},
        {"prompt": "a", "input_file": Path("input.json")},
        {"prompt": None, "prompt_file": Path("positive.txt"), "input_file": Path("input.json")},
    ],
)
def test_conflicting_sources_are_rejected_without_opening_any_file(kwargs):
    with pytest.raises(KomaquillError) as error:
        image_input(**kwargs)
    assert error.value.code == "invalid_request"
