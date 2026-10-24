"""Check our pinned executor integration without testing Jupyter's internals."""

from types import SimpleNamespace

import pytest

from komaquill.infrastructure import colab_execution


@pytest.mark.parametrize("failure", [None, "readiness", "execution", "cleanup"])
def test_executor_waits_once_does_not_retry_and_closes_failed_connections(monkeypatch, failure):
    calls = []

    def wait(**kwargs):
        calls.append(("ready", kwargs))
        if failure == "readiness":
            raise TimeoutError("private transport message")

    class Runtime:
        def __init__(self):
            self.kernel_client = SimpleNamespace(
                _manager=SimpleNamespace(client=SimpleNamespace(wait_for_ready=wait))
            )

        def execute_code(self, code, **kwargs):
            calls.append(("execute", code))
            if failure in {"execution", "cleanup"}:
                raise RuntimeError("private response")
            return []

        def stop(self):
            calls.append(("stop",))
            if failure == "cleanup":
                raise OSError("private cleanup failure")

    module = SimpleNamespace(ColabRuntime=Runtime)
    monkeypatch.setattr(colab_execution.importlib, "import_module", lambda _: module)
    with colab_execution.execution_transport(87):
        runtime = module.ColabRuntime()
        if failure:
            with pytest.raises((TimeoutError, RuntimeError)):
                runtime.execute_code("payload")
            assert calls[-1] == ("stop",)
        else:
            runtime.execute_code("preflight")
            runtime.execute_code("payload")
    assert module.ColabRuntime is Runtime
    assert calls[0] == ("ready", {"timeout": 87})
    assert sum(row[0] == "ready" for row in calls) == 1
    assert sum(row[0] == "execute" for row in calls) == (
        0 if failure == "readiness" else 1 if failure else 2
    )
