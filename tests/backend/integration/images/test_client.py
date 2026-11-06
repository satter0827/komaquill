"""Verify images contracts within the declared test scope."""

import pytest
from komaquill.domain.errors import KomaquillError


@pytest.mark.parametrize("queue", [{"queue_running": None}, {"queue_pending": [None]}])
def test_invalid_queue_raises_safe_operation_error(client, stub, queue):
    stub.queue = queue
    with pytest.raises(KomaquillError):
        client.queue()
