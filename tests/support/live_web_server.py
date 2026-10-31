"""Serve an explicitly selected live verification workspace without test substitutes."""

import argparse
from pathlib import Path

import uvicorn
from komaquill.api import Client
from komaquill.interfaces.http.app import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    target = args.workspace.resolve()
    if not target.is_relative_to(root / ".artifacts/records/reports"):
        raise ValueError("Live verification requires an isolated report workspace.")
    uvicorn.run(
        create_app(Client(target), ui_directory=root / ".artifacts/build/frontend"),
        host="127.0.0.1",
        port=8767,
        access_log=False,
    )


if __name__ == "__main__":
    main()
