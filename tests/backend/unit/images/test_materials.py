"""Verify images contracts within the declared test scope."""

import pytest

from tests.backend.support.materials_fixtures import material


@pytest.mark.parametrize(
    "reference", ["../escape.png", "/absolute.png", "C:/input.png", "bad\\input.png"]
)
def test_material_references_cannot_redirect_storage(reference):
    with pytest.raises(ValueError):
        material(name=reference)
