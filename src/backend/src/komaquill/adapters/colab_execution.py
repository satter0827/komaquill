"""Guard the pinned official executor with kernel readiness and failure cleanup."""

import importlib
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any


@contextmanager
def execution_transport(timeout: float) -> Iterator[None]:
    """Wait through cold WebSocket setup without resending any execution request."""
    module: Any = importlib.import_module("colab_cli.commands.execution")
    manager: Any = importlib.import_module("jupyter_kernel_client.manager")
    original = module.ColabRuntime
    original_fetch = manager.fetch
    readiness_deadline: float | None = None

    def fetch(*args: Any, **kwargs: Any) -> Any:
        # The pinned Jupyter client otherwise uses a fixed 10-second HTTP budget
        # before our WebSocket readiness guard is reached. Never replay a POST.
        if readiness_deadline is not None and kwargs.get("method", "GET").upper() == "GET":
            remaining = readiness_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            kwargs["timeout"] = remaining
        return original_fetch(*args, **kwargs)

    def runtime(*args: Any, **kwargs: Any) -> Any:
        instance = original(*args, **kwargs)
        execute = instance.execute_code
        ready = False

        def execute_ready(*arguments: Any, **options: Any) -> Any:
            nonlocal ready, readiness_deadline
            try:
                if not ready:
                    readiness_deadline = time.monotonic() + timeout
                    # CLI 0.7.5 does not expose its WebSocket readiness timeout.
                    # Keep this pinned private dependency in the execution adapter.
                    connection = instance.kernel_client._manager.client
                    remaining = readiness_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    connection.wait_for_ready(timeout=remaining)
                    ready = True
                    readiness_deadline = None
                return execute(*arguments, **options)
            except BaseException:
                with suppress(Exception):
                    instance.stop()
                raise
            finally:
                readiness_deadline = None

        instance.execute_code = execute_ready
        return instance

    module.ColabRuntime = runtime
    manager.fetch = fetch
    try:
        yield
    finally:
        module.ColabRuntime = original
        manager.fetch = original_fetch
