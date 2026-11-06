"""Verify locked wheel installation, cache invalidation and failure isolation."""

import subprocess

import pytest

from tools.testing import browser_environment as browser


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
