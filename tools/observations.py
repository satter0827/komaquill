"""Describe process ownership and append observations without rewriting run records."""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any

import psutil

from tools.reporting import ROOT, atomic_json


def owner(identity: int | None = None) -> dict[str, int | float]:
    process = psutil.Process(os.getpid() if identity is None else identity)
    return {"owner_pid": process.pid, "owner_started": process.create_time()}


def owner_state(value: dict[str, Any]) -> str:
    if type(value.get("owner_pid")) is not int or type(value.get("owner_started")) not in {
        int,
        float,
    }:
        return "unknown"
    if (
        value["owner_pid"] <= 0
        or not isfinite(value["owner_started"])
        or value["owner_started"] <= 0
    ):
        return "unknown"
    try:
        return (
            "alive"
            if psutil.Process(value["owner_pid"]).create_time() == value["owner_started"]
            else "reused"
        )
    except psutil.NoSuchProcess:
        return "dead"
    except psutil.Error:
        return "unknown"


def observe(root: Path = ROOT) -> None:
    records = root / ".artifacts/records/reports"
    paths = [*(records / "checks").glob("*/checks.json"), *(records / "web").glob("*/run.json")]
    for path in paths:
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("status") != "running":
                continue
            state = owner_state(value)
            digest = hashlib.sha256(raw).hexdigest()
            target = path.parent / "observations" / f"{digest}-{state}.json"
            if target.exists():
                continue
            atomic_json(
                target,
                {
                    "format_version": 1,
                    "source_file": path.name,
                    "source_sha256": digest,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "owner_state": state,
                    "observed_status": "interrupted" if state in {"dead", "reused"} else state,
                },
            )
        except (OSError, ValueError):
            continue


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--observe", action="store_true")
    args = parser.parse_args()
    if args.observe:
        observe()
    print(json.dumps(owner(args.owner_pid)))
