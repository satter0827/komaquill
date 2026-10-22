"""公式Colab CLIを詳細ログと秘密情報を含む例外表示なしで起動する。"""

import importlib
import logging
import sys

import click


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
    except Exception:
        print("Colabの操作に失敗しました。認証と接続状態を確認してください。", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
