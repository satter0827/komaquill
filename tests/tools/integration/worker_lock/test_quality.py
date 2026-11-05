"""Verify quality contracts within the declared test scope."""

from pathlib import Path

import pytest

from tools.checks import worker_lock as check_worker_lock


@pytest.mark.parametrize("case", ["matching", "different", "missing_uv"])
def test_gpu_lock_gate_detects_drift_without_resolving_dependencies(tmp_path, monkeypatch, case):
    resource = tmp_path / "src/backend/src/komaquill/resources/runtime/pylock.worker.toml"
    resource.parent.mkdir(parents=True)
    resource.write_text("locked\n")
    monkeypatch.setattr(
        check_worker_lock, "__file__", str(tmp_path / "tools/checks/worker_lock.py")
    )
    if case == "missing_uv":
        monkeypatch.delenv("KOMAQUILL_UV", raising=False)
        monkeypatch.setattr(check_worker_lock.shutil, "which", lambda _: None)
    else:
        monkeypatch.setenv("KOMAQUILL_UV", "uv-test")

    def export(args, **kwargs):
        assert "--locked" in args and kwargs["check"]
        Path(args[args.index("--output-file") + 1]).write_text(
            "different\n" if case == "different" else "locked\n"
        )

    monkeypatch.setattr(check_worker_lock.subprocess, "run", export)
    if case == "matching":
        check_worker_lock.main()
    else:
        with pytest.raises(SystemExit):
            check_worker_lock.main()
