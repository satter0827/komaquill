"""Exercise shutdown decisions independently of files, subprocesses and providers."""

from uuid import uuid4

import pytest
from komaquill.images.generation import Collection
from komaquill.runtime.lifecycle import ShutdownActions, shutdown

from tests.support.provider import stopped as confirmed_stop


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
            stop=action("stop", confirmed_stop()),
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


@pytest.mark.parametrize("case", ["saved", "partial", "capture_failed", "finalize_failed"])
def test_material_preservation_does_not_prevent_gpu_shutdown(case):
    steps = []

    def preserve():
        if case == "capture_failed":
            raise OSError("private-error")
        return ("material-id",) if case == "partial" else ()

    def finalize():
        if case == "finalize_failed":
            raise OSError("private-error")
        return ("material-id",) if case in {"partial", "capture_failed"} else ()

    actions = ShutdownActions(
        collect=lambda: Collection(0, 0, 0),
        logs=lambda: (),
        stop=lambda: steps.append("gpu_stop") or confirmed_stop(),
        disconnect=lambda: None,
        finalize=lambda: (),
        persist=lambda result: steps.append("persist"),
        release=lambda: steps.append("release"),
        display=lambda result: None,
        forced=lambda: False,
        preserve=preserve,
        finalize_materials=finalize,
    )
    result, released = shutdown(uuid4(), False, actions)
    assert "gpu_stop" in steps and result.gpu == "stopped"
    assert result.materials == {"partial": "incomplete", "capture_failed": "failed"}.get(case, "ok")
    assert released is (case != "finalize_failed")
    assert "private-error" not in result.model_dump_json()
