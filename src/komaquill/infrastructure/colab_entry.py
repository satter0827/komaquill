"""Invoke the official CLI without verbose logs or credential-bearing tracebacks."""

import importlib
import json
import logging
import os
import sys
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

import click
import typer

from komaquill.domain.errors import ErrorCode, FailureInfo, OperationStage
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import record_event
from komaquill.messages import message


def main() -> None:
    """Use the official argument and authentication flows."""
    logging.disable(logging.CRITICAL)
    cli = importlib.import_module("colab_cli.cli")
    cli.app.pretty_exceptions_enable = False
    try:
        result = cli.app(standalone_mode=False)
        raise SystemExit(result if isinstance(result, int) else 0)
    except (click.exceptions.Exit, typer.Exit) as error:
        raise SystemExit(error.exit_code) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        root = os.environ.get("KOMAQUILL_PROJECT_DIR")
        if root:
            # Do not store exception text, arguments, locals or HTTP bodies.
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            runtime_id = None
            try:
                receipt = Path(root) / ".artifacts/state/colab.json"
                runtime_id = str(UUID(json.loads(receipt.read_bytes())["runtime_id"]))
            except (OSError, ValueError, KeyError, TypeError):
                pass
            actions = {"new", "status", "stop", "exec", "ssh", "upload", "download"}
            function = None
            trace = error.__traceback__
            while trace is not None:
                filename = trace.tb_frame.f_code.co_filename.replace("\\", "/")
                if "/colab_cli/" in filename:
                    function = trace.tb_frame.f_code.co_name
                trace = trace.tb_next
            record_event(
                Project(Path(root)),
                "colab_" + next((value for value in sys.argv[1:] if value in actions), "operation"),
                "failed",
                phase="official_cli",
                sdk_version=version("google-colab-cli"),
                function=function,
                runtime_id=UUID(runtime_id) if runtime_id else None,
                failure=FailureInfo(
                    code=ErrorCode.COLAB_FAILED,
                    stage=OperationStage.COLAB_CONTROL,
                    exception_type=type(error).__name__,
                    http_status=status if isinstance(status, int) else None,
                ),
            )
        print(
            message("text.the_colab_operation_failed_check_authentication_and_connection"),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
