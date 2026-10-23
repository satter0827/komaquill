"""Exercise shutdown decisions independently of files, subprocesses and providers."""

from uuid import uuid4

import pytest

from komaquill.application.generation import Collection
from komaquill.application.lifecycle import ShutdownActions, shutdown


@pytest.mark.parametrize(
    "failure",
    [None, "collect", "logs", "stop", "disconnect", "finalize", "persist", "release", "display"],
)
def test_shutdown_attempts_gpu_stop_and_keeps_unfinished_ownership(failure):
    steps = []

    def action(name, result=None):
        def run(*args):
            steps.append(name)
            if name == failure:
                raise RuntimeError("private-response")
            return result

        return run

    result, released = shutdown(
        uuid4(),
        True,
        ShutdownActions(
            collect=action("collect", Collection(0, 0, 0)),
            logs=action("logs", ()),
            stop=action("stop"),
            disconnect=action("disconnect"),
            finalize=action("finalize", ()),
            persist=action("persist"),
            release=action("release"),
            display=action("display"),
            forced=lambda: False,
        ),
    )
    assert "stop" in steps and "disconnect" in steps
    assert released == (failure is None)
    assert bool(result.errors) == (failure is not None)
    assert "private-response" not in result.model_dump_json()
    assert (result.gpu == "stopped") == (failure != "stop")
