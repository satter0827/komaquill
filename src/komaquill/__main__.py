"""Launch the public CLI without importing it before startup error handling."""

import sys


def main() -> None:
    """Keep catalog and import failures free of credential-bearing tracebacks."""
    from komaquill.messages import MessageCatalogError

    try:
        from komaquill.interfaces.cli import app

        app()
    except MessageCatalogError:
        print("Message definitions are unavailable. Reinstall Komaquill.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
