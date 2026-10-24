"""Guard the pinned official executor with kernel readiness and failure cleanup."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any


@contextmanager
def execution_transport(timeout: float) -> Iterator[None]:
    """Wait through cold WebSocket setup without resending any execution request."""
    module: Any = importlib.import_module("colab_cli.commands.execution")
    original = module.ColabRuntime

    def runtime(*args: Any, **kwargs: Any) -> Any:
        instance = original(*args, **kwargs)
        execute = instance.execute_code
        ready = False

        def execute_ready(*arguments: Any, **options: Any) -> Any:
            nonlocal ready
            try:
                if not ready:
                    # CLI 0.7.5 does not expose its WebSocket readiness timeout.
                    # Keep this pinned private dependency in the execution adapter.
                    instance.kernel_client._manager.client.wait_for_ready(timeout=timeout)
                    ready = True
                return execute(*arguments, **options)
            except BaseException:
                with suppress(Exception):
                    instance.stop()
                raise

        instance.execute_code = execute_ready
        return instance

    module.ColabRuntime = runtime
    try:
        yield
    finally:
        module.ColabRuntime = original
