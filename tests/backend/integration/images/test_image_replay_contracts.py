"""Verify images contracts within the declared test scope."""

import pytest
from komaquill.application.images.generation import generate
from komaquill.domain.errors import KomaquillError


@pytest.mark.parametrize("seed", [True, 0.5, -1, 2**64])
def test_single_image_seed_is_rejected_before_backend_or_history(
    project, session, client, stub, seed
):
    active = session.model_copy(update={"settings": project.settings()})
    with pytest.raises(KomaquillError) as error:
        generate(project, client, active, "a cat", seed)
    assert error.value.code == "invalid_request"
    assert stub.submissions == 0 and project.repository.load() == []


@pytest.mark.parametrize("prompt,subjects", [(42, ()), ("a cat", (42,))])
def test_invalid_python_input_types_are_safe_and_do_not_submit(
    project, session, client, stub, prompt, subjects
):
    active = session.model_copy(update={"settings": project.settings()})
    with pytest.raises(KomaquillError) as error:
        generate(project, client, active, prompt, 42, subjects=subjects)
    assert error.value.code == "invalid_request"
    assert stub.submissions == 0 and project.repository.load() == []
