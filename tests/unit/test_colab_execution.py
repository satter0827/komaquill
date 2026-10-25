"""Check our pinned executor integration without testing Jupyter's internals."""

from types import SimpleNamespace

import pytest

from komaquill.infrastructure import colab_execution


def test_cold_http_discovery_uses_control_deadline_without_replaying_execution(monkeypatch):
    calls = []

    def fetch(url, **kwargs):
        calls.append((kwargs["method"], kwargs["timeout"]))

    manager = SimpleNamespace(fetch=fetch)

    class Runtime:
        @property
        def kernel_client(self):
            manager.fetch("private", method="GET", timeout=10)
            manager.fetch("private", method="POST", timeout=10)
            return SimpleNamespace(
                _manager=SimpleNamespace(client=SimpleNamespace(wait_for_ready=lambda **kw: None))
            )

        def execute_code(self, code):
            calls.append(("execute", code))

        def stop(self):
            pass

    module = SimpleNamespace(ColabRuntime=Runtime)
    monkeypatch.setattr(
        colab_execution.importlib,
        "import_module",
        lambda name: manager if name.endswith("manager") else module,
    )
    monkeypatch.setattr(colab_execution.time, "monotonic", lambda: 100)
    with colab_execution.execution_transport(90):
        module.ColabRuntime().execute_code("payload")
        manager.fetch("private", method="GET", timeout=10)
    assert calls == [("GET", 90), ("POST", 10), ("execute", "payload"), ("GET", 10)]
    assert manager.fetch is fetch


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
    manager = SimpleNamespace(fetch=lambda *args, **kwargs: None)
    monkeypatch.setattr(
        colab_execution.importlib,
        "import_module",
        lambda name: module if name == "colab_cli.commands.execution" else manager,
    )
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
    assert calls[0][0] == "ready" and calls[0][1]["timeout"] == pytest.approx(87, abs=0.1)
    assert sum(row[0] == "ready" for row in calls) == 1
    assert sum(row[0] == "execute" for row in calls) == (
        0 if failure == "readiness" else 1 if failure else 2
    )
