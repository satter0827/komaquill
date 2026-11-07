"""Verify locked wheel installation, cache invalidation and failure isolation."""

import json
import os
import subprocess

import pytest

from tools.testing import browser_environment as browser


def generation(tmp_path, index):
    path = tmp_path / ".artifacts/runtime/browser-tests" / (str(index) * 64)
    path.mkdir(parents=True)
    (path / ".installed-locked").write_text(path.name)
    (path / ".generation.json").write_text(json.dumps({"format_version": 1, "digest": path.name}))
    os.utime(path / ".installed-locked", (index, index))
    return path


def test_retention_keeps_current_previous_active_unknown_and_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "ROOT", tmp_path)
    old, active, unknown, previous, current = [generation(tmp_path, i) for i in range(1, 6)]
    lease = active / ".leases/process.json"
    lease.parent.mkdir()
    from tools.observations import owner

    identity = owner()
    lease.write_text(
        json.dumps({"pid": identity["owner_pid"], "started": identity["owner_started"]})
    )
    (unknown / ".generation.json").unlink()
    incomplete = current.parent / ("6" * 64)
    incomplete.mkdir()
    browser.prune(current)
    assert not old.exists()
    assert all(path.exists() for path in (active, unknown, previous, current, incomplete))
    saved = json.loads((current.parent / "retention.json").read_bytes())
    assert {r["status"] for r in saved["generations"]} == {"removed", "active", "unknown"}


@pytest.mark.parametrize("lease", [{"pid": -1, "started": 1}, {"pid": "unknown"}, []])
def test_uncertain_leases_are_retained(tmp_path, monkeypatch, lease):
    monkeypatch.setattr(browser, "ROOT", tmp_path)
    old, _, current = [generation(tmp_path, i) for i in range(1, 4)]
    path = old / ".leases/unknown.json"
    path.parent.mkdir()
    path.write_text(json.dumps(lease))
    browser.prune(current)
    assert old.exists()


def test_dead_lease_is_removed_but_external_link_target_is_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "ROOT", tmp_path)
    old, _, current = [generation(tmp_path, i) for i in range(1, 4)]
    outside = tmp_path / "original"
    outside.mkdir()
    (outside / "keep").write_text("original")
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(old / "linked"), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        (old / "linked").symlink_to(outside, target_is_directory=True)
    browser.prune(current)
    assert not old.exists() and (outside / "keep").read_text() == "original"


def test_invalid_retention_and_outside_root_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "ROOT", tmp_path)
    current = generation(tmp_path, 1)
    with pytest.raises(ValueError):
        browser.prune(tmp_path / "outside")
    monkeypatch.setattr(browser, "policy", lambda _: {"browser_environment_retention_count": 0})
    with pytest.raises(ValueError):
        browser.prune(current)


def test_environment_is_cached_by_wheel_lock_and_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "ROOT", tmp_path)
    dist = tmp_path / ".artifacts/dist"
    dist.mkdir(parents=True)
    lock = tmp_path / "src/backend/uv.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("locked")
    wheel = dist / "komaquill-1.0.whl"
    wheel.write_bytes(b"wheel")
    monkeypatch.setenv("KOMAQUILL_UV", "fixture-uv")
    calls = []

    def installed(command, **kw):
        calls.append((command, kw))
        if command[1] == "venv":
            from pathlib import Path

            Path(command[-1]).mkdir(parents=True)

    monkeypatch.setattr(browser.subprocess, "run", installed)
    first = browser.prepare()
    assert len(calls) == 4
    assert all(call[1]["check"] and call[1]["timeout"] for call in calls)
    assert "--no-index" in calls[1][0] and "--no-deps" in calls[1][0]
    assert "--require-hashes" in calls[3][0]
    assert browser.prepare() == first and len(calls) == 4
    lock.write_text("new lock")
    assert browser.prepare() != first and len(calls) == 8
    wheel.write_bytes(b"changed wheel")
    assert browser.prepare() != first and len(calls) == 12


def test_failed_install_is_not_cached_and_ambiguous_wheels_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "ROOT", tmp_path)
    with pytest.raises(RuntimeError):
        browser.prepare()
    dist = tmp_path / ".artifacts/dist"
    dist.mkdir(parents=True)
    for name in ("a", "b"):
        (dist / f"komaquill-{name}.whl").touch()
    with pytest.raises(RuntimeError):
        browser.prepare()
    (dist / "komaquill-b.whl").unlink()
    lock = tmp_path / "src/backend/uv.lock"
    lock.parent.mkdir(parents=True)
    lock.touch()
    monkeypatch.delenv("KOMAQUILL_UV", raising=False)
    monkeypatch.setattr(browser.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        browser.prepare()
    monkeypatch.setenv("KOMAQUILL_UV", "fixture-uv")

    def fail(*args, **kw):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(browser.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        browser.prepare()
    assert not list((tmp_path / ".artifacts/runtime").rglob(".installed-locked"))
