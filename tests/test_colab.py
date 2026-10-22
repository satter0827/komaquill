"""確保・接続・停止の所有範囲を検証する。"""

import json
from uuid import uuid4

import pytest

from komaquill.colab import Colab, Receipt
from komaquill.errors import KomaquillError
from komaquill.storage import atomic_json


def controller(project, monkeypatch):
    project.runtime.mkdir(parents=True)
    colab = Colab(project)
    colab.config = project.runtime / "official-sessions.json"
    calls = []
    monkeypatch.setattr(colab, "call", lambda *args, **kwargs: calls.append(args))
    return colab, calls


def test_missing_session_never_calls_ssh_or_allocates(project, monkeypatch):
    colab, calls = controller(project, monkeypatch)
    receipt = Receipt(name="owned", runtime_id=uuid4(), source_revision="a" * 40)
    with pytest.raises(KomaquillError, match="ランタイムがありません"):
        colab.tunnel(receipt)
    assert not calls


def test_runtime_id_mismatch_prevents_stop(project, monkeypatch):
    colab, calls = controller(project, monkeypatch)
    receipt = Receipt(
        name="owned", runtime_id=uuid4(), source_revision="a" * 40, endpoint_id="expected"
    )
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    atomic_json(colab.config, {"owned": {"endpoint": "other-runtime"}})
    with pytest.raises(KomaquillError, match="ランタイムID"):
        colab.stop()
    assert not calls
    assert colab.receipt_file.is_file()


def test_failed_stop_keeps_receipt(project, monkeypatch):
    colab, calls = controller(project, monkeypatch)
    receipt = Receipt(
        name="owned", runtime_id=uuid4(), source_revision="a" * 40, endpoint_id="expected"
    )
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    atomic_json(colab.config, {"owned": {"endpoint": "expected"}})
    with pytest.raises(KomaquillError, match="終了を確認できません"):
        colab.stop()
    assert ("stop", "-s", "owned") in calls
    assert colab.receipt_file.is_file()
    assert not any(args[0] == "new" for args in calls)


def test_stop_removes_receipt_only_after_confirming_absence(project, monkeypatch):
    colab, calls = controller(project, monkeypatch)
    receipt = Receipt(
        name="owned", runtime_id=uuid4(), source_revision="a" * 40, endpoint_id="expected"
    )
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    atomic_json(colab.config, {"owned": {"endpoint": "expected"}})

    def call(*args, **kwargs):
        calls.append(args)
        if args[0] == "stop":
            colab.config.write_text(json.dumps({}))

    monkeypatch.setattr(colab, "call", call)
    colab.stop()
    assert not colab.receipt_file.exists()
    assert not any(args[0] == "new" for args in calls)


def test_official_cli_entrypoint_does_not_create_a_runtime(project):
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "komaquill.colab_entry", "version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "Version: 0.7.5"
    assert not project.runtime.exists()


def test_official_config_parent_is_created_before_cli_initializes_locks(project):
    colab = Colab(project)
    colab.config = project.runtime / "not-created/official/sessions.json"
    command = colab.command("status")
    assert colab.config.parent.is_dir()
    assert command[-1] == "status"


def test_preflight_failure_never_allocates_or_leaves_uncertain_receipt(project, monkeypatch):
    colab, calls = controller(project, monkeypatch)

    def fail(*args, **kwargs):
        calls.append(args)
        raise KomaquillError("確認に失敗しました")

    monkeypatch.setattr(colab, "call", fail)
    with pytest.raises(KomaquillError):
        colab.acquire("a" * 40)
    assert not any(args[0] == "new" for args in calls)
    assert not colab.receipt_file.is_file()


def test_uncertain_allocation_is_not_reported_as_terminated(project, monkeypatch):
    colab, calls = controller(project, monkeypatch)
    receipt = Receipt(name="owned", runtime_id=uuid4(), source_revision="a" * 40)
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    with pytest.raises(KomaquillError, match="確保結果を確認できません"):
        colab.stop()
    assert colab.receipt_file.is_file()
    assert not any(args[0] in {"new", "stop"} for args in calls)


def test_exception_diagnostic_never_contains_message_or_locals(project, monkeypatch, capsys):
    import types

    from komaquill import colab_entry

    class FailingApp:
        pretty_exceptions_enable = True

        def __call__(self, **kwargs):
            private_value = "should-stay-private"
            raise RuntimeError(private_value)

    fake = types.SimpleNamespace(app=FailingApp())
    monkeypatch.setenv("KOMAQUILL_PROJECT_DIR", str(project.root))
    monkeypatch.setattr(colab_entry.importlib, "import_module", lambda name: fake)
    with pytest.raises(SystemExit) as error:
        colab_entry.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    diagnostic = (project.artifacts / "logs/colab-cli-error.json").read_text(encoding="utf-8")
    assert "should-stay-private" not in output.out + output.err + diagnostic
    assert json.loads(diagnostic)["type"] == "RuntimeError"


def test_owned_child_processes_are_terminated_together(tmp_path):
    import subprocess
    import sys
    import time
    from contextlib import suppress

    import psutil

    from komaquill.colab import terminate

    marker = tmp_path / "child.pid"
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent_code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.is_file():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child_id = int(marker.read_text())
        terminate(process, 10)
        assert process.poll() is not None
        with suppress(psutil.NoSuchProcess):
            assert psutil.Process(child_id).status() == psutil.STATUS_ZOMBIE
    finally:
        if process.poll() is None:
            terminate(process, 10)
