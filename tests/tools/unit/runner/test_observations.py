"""Keep process observations separate from retained execution records."""

import json
import os
from types import SimpleNamespace

import psutil
import pytest

from tools import observations


def test_owner_and_observation_preserve_original_bytes(tmp_path):
    identity = observations.owner()
    assert identity["owner_pid"] == os.getpid()
    path = tmp_path / ".artifacts/records/reports/checks/run/checks.json"
    path.parent.mkdir(parents=True)
    raw = json.dumps({"status": "running", **identity}).encode()
    path.write_bytes(raw)
    observations.observe(tmp_path)
    observations.observe(tmp_path)
    assert path.read_bytes() == raw
    records = list((path.parent / "observations").glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_bytes())["observed_status"] == "alive"


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"owner_pid": True, "owner_started": 1},
        {"owner_pid": -1, "owner_started": 1},
        {"owner_pid": 1, "owner_started": float("nan")},
    ],
)
def test_invalid_ownership_is_unknown(value):
    assert observations.owner_state(value) == "unknown"


@pytest.mark.parametrize("state", ["dead", "reused", "unknown"])
def test_process_identity_is_checked_before_interruption_is_reported(tmp_path, monkeypatch, state):
    def process(pid):
        if state == "dead":
            raise psutil.NoSuchProcess(pid)
        if state == "unknown":
            raise psutil.AccessDenied(pid)
        return SimpleNamespace(create_time=lambda: 2)

    monkeypatch.setattr(observations.psutil, "Process", process)
    path = tmp_path / ".artifacts/records/reports/web/run/run.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "running", "owner_pid": 123, "owner_started": 1}))
    observations.observe(tmp_path)
    record = json.loads(next((path.parent / "observations").glob("*.json")).read_bytes())
    assert record["owner_state"] == state
    assert record["observed_status"] == ("unknown" if state == "unknown" else "interrupted")


def test_bad_or_finished_records_are_not_rewritten(tmp_path, monkeypatch):
    root = tmp_path / ".artifacts/records/reports/checks"
    for name, value in [("broken", b"{"), ("list", b"[]"), ("done", b'{"status":"passed"}')]:
        path = root / name / "checks.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(value)
    observations.observe(tmp_path)
    assert not list(root.rglob("observations"))
