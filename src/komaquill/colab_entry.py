"""公式Colab CLIを詳細ログと秘密情報を含む例外表示なしで起動する。"""

import importlib
import json
import logging
import os
import sys
from pathlib import Path
from uuid import UUID

import click

from komaquill.events import record_event
from komaquill.models import FailureInfo
from komaquill.settings import Project


def main() -> None:
    """公式CLIの引数と認証手順を使用する。"""
    logging.disable(logging.CRITICAL)
    cli = importlib.import_module("colab_cli.cli")
    cli.app.pretty_exceptions_enable = False
    try:
        cli.app(standalone_mode=False)
    except click.exceptions.Exit as error:
        raise SystemExit(error.exit_code) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        root = os.environ.get("KOMAQUILL_PROJECT_DIR")
        if root:
            # 例外の本文、引数、ローカル変数、HTTP本文は保存しない。
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            runtime_id = None
            try:
                receipt = Path(root) / ".artifacts/state/colab.json"
                runtime_id = str(UUID(json.loads(receipt.read_bytes())["runtime_id"]))
            except (OSError, ValueError, KeyError, TypeError):
                pass
            actions = {"new", "status", "stop", "exec", "ssh", "upload", "download"}
            record_event(
                Project(Path(root)),
                "colab_" + next((value for value in sys.argv[1:] if value in actions), "operation"),
                "failed",
                runtime_id=UUID(runtime_id) if runtime_id else None,
                failure=FailureInfo(
                    code="colab_failed",
                    stage="colab_control",
                    exception_type=type(error).__name__,
                    http_status=status if isinstance(status, int) else None,
                ),
            )
        print("Colabの操作に失敗しました。認証と接続状態を確認してください。", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
