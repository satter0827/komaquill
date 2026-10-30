"""Export or check the side-effect-free HTTP contract."""

import argparse
import json
from pathlib import Path

from komaquill.interfaces.http.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    content = json.dumps(create_app().openapi(), indent=2) + "\n"
    path = ROOT / "src/contracts/openapi.json"
    if options.check:
        if path.read_text(encoding="utf-8") != content:
            raise SystemExit("Regenerate the OpenAPI contract with pnpm contracts.")
    else:
        path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
