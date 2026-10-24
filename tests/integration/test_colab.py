"""Test allocation, connection and shutdown ownership."""

import json
from uuid import uuid4

import pytest

from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.colab import Colab, Receipt
from komaquill.infrastructure.storage import atomic_json


def controller(project, monkeypatch):
    project.runtime.mkdir(parents=True)
    colab = Colab(project)
    colab.config = project.runtime / "official-sessions.json"
    calls = []

    def provider_call(action, *args, **kwargs):
        calls.append((action, *args))
        saved = colab.receipt()
        from komaquill.domain.models import RuntimeObservation

        if saved is None or saved.endpoint_id is None:
            raise KomaquillError(code="identity_mismatch")
        return RuntimeObservation(
            state="running" if colab.entry(saved) is not None else "stopped",
            endpoint_id=saved.endpoint_id,
            account_sha256=saved.account_sha256,
        )

    monkeypatch.setattr(colab, "provider_call", provider_call)
    monkeypatch.setattr(colab, "call", lambda *args, **kwargs: calls.append(args))
    return colab, calls


def test_missing_session_never_calls_ssh_or_allocates(project, monkeypatch):
    colab, calls = controller(project, monkeypatch)
    receipt = Receipt(name="owned", runtime_id=uuid4(), source_revision="a" * 40)
    with pytest.raises(KomaquillError):
        colab.tunnel(receipt)
    assert not calls


def test_cached_identity_mismatch_is_rejected_before_cli_operations(project, monkeypatch):
    colab, calls = controller(project, monkeypatch)
    receipt = Receipt(
        name="owned",
        runtime_id=uuid4(),
        source_revision="a" * 40,
        endpoint_id="expected",
        account_sha256="a" * 64,
    )
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    atomic_json(colab.config, {"owned": {"endpoint": "other-runtime"}})
    with pytest.raises(KomaquillError):
        colab.entry(receipt)
    assert not calls
    assert colab.receipt_file.is_file()


def test_failed_stop_keeps_receipt(project, monkeypatch):
    colab, calls = controller(project, monkeypatch)
    receipt = Receipt(
        name="owned",
        runtime_id=uuid4(),
        source_revision="a" * 40,
        endpoint_id="expected",
        account_sha256="a" * 64,
    )
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    atomic_json(colab.config, {"owned": {"endpoint": "expected"}})
    with pytest.raises(KomaquillError):
        colab.stop()
    assert ("terminate",) in calls
    assert colab.receipt_file.is_file()
    assert not any(args[0] == "new" for args in calls)


def test_stop_keeps_receipt_until_finalization_acknowledges_absence(project, monkeypatch):
    colab, calls = controller(project, monkeypatch)
    receipt = Receipt(
        name="owned",
        runtime_id=uuid4(),
        source_revision="a" * 40,
        endpoint_id="expected",
        account_sha256="a" * 64,
    )
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    atomic_json(colab.config, {"owned": {"endpoint": "expected"}})

    def call(*args, **kwargs):
        calls.append(args)
        from tests.support.provider import stopped

        if args[0] == "terminate":
            colab.config.write_text(json.dumps({}))
        return stopped()

    monkeypatch.setattr(colab, "provider_call", call)
    colab.stop()
    assert colab.receipt_file.exists()
    colab.release(receipt)
    assert not colab.receipt_file.exists()
    assert not any(args[0] == "new" for args in calls)


def test_official_cli_entrypoint_does_not_create_a_runtime(project):
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "komaquill.infrastructure.colab_entry", "version"],
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
    with pytest.raises(KomaquillError):
        colab.stop()
    assert colab.receipt_file.is_file()
    assert not any(args[0] in {"new", "stop"} for args in calls)


def test_exception_diagnostic_never_contains_message_or_locals(project, monkeypatch, capsys):
    import types

    from komaquill.infrastructure import colab_entry as colab_entry

    class FailingApp:
        pretty_exceptions_enable = True

        def __call__(self, **kwargs):
            private_value = "should-stay-private"
            raise RuntimeError(private_value)

    fake = types.SimpleNamespace(app=FailingApp())
    monkeypatch.setenv("KOMAQUILL_PROJECT_DIR", str(project.root))
    original_import = colab_entry.importlib.import_module
    monkeypatch.setattr(
        colab_entry.importlib,
        "import_module",
        lambda name: fake if name == "colab_cli.cli" else original_import(name),
    )
    with pytest.raises(SystemExit) as error:
        colab_entry.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    diagnostic = (project.logs / "cli.jsonl").read_text(encoding="utf-8")
    assert "should-stay-private" not in output.out + output.err + diagnostic
    assert json.loads(diagnostic)["failure"]["exception_type"] == "RuntimeError"


def test_owned_child_processes_are_terminated_together(tmp_path):
    import subprocess
    import sys
    import time
    from contextlib import suppress

    import psutil

    from komaquill.infrastructure.colab import terminate

    marker = tmp_path / "child.pid"
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"Path({str(marker) + '.tmp'!r}).write_text(str(child.pid)); "
        f"Path({str(marker) + '.tmp'!r}).replace({str(marker)!r}); time.sleep(60)"
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


def test_cleanup_after_monitor_crash_only_stops_owned_official_processes(project, monkeypatch):
    from types import SimpleNamespace

    from komaquill.infrastructure import colab as module

    colab, _ = controller(project, monkeypatch)
    receipt = Receipt(name="owned", runtime_id=uuid4(), source_revision="a" * 40)

    def process(config, name, module_name="komaquill.infrastructure.colab_entry"):
        return SimpleNamespace(
            info={
                "cmdline": [
                    "python",
                    "-m",
                    module_name,
                    "--config",
                    str(config),
                    "exec",
                    "-s",
                    name,
                ]
            }
        )

    owned = process(colab.config, receipt.name)
    others = [
        process(project.runtime / "other.json", receipt.name),
        process(colab.config, "another-session"),
        process(colab.config, receipt.name, "another_module"),
    ]
    monkeypatch.setattr(module.psutil, "process_iter", lambda attrs: [owned, *others])
    stopped = []
    monkeypatch.setattr(module, "terminate_tree", lambda process, timeout: stopped.append(process))
    colab.cleanup_processes(receipt)
    assert stopped == [owned]


def test_release_never_removes_a_replacement_receipt(project, monkeypatch):
    colab, _ = controller(project, monkeypatch)
    receipt = Receipt(name="owned", runtime_id=uuid4(), source_revision="a" * 40)
    replacement = receipt.model_copy(update={"runtime_id": uuid4()})
    atomic_json(colab.receipt_file, replacement.model_dump(mode="json"))
    with pytest.raises(KomaquillError):
        colab.release(receipt)
    assert colab.receipt().runtime_id == replacement.runtime_id


def test_log_failure_preserves_existing_file_and_downloads_remaining_logs(project, monkeypatch):
    colab, _ = controller(project, monkeypatch)
    receipt = Receipt(name="owned", runtime_id=uuid4(), source_revision="a" * 40)
    folder = project.logs / str(receipt.runtime_id)
    folder.mkdir(parents=True)
    (folder / "bootstrap.log").write_text("previous-complete-log")
    monkeypatch.setattr(colab, "entry", lambda receipt: {"endpoint": "expected"})

    def download(*args, **kwargs):
        from pathlib import Path

        target = Path(args[-1])
        target.write_text("downloaded")
        if "bootstrap.log" in args[-2]:
            raise KomaquillError("取得に失敗しました")

    monkeypatch.setattr(colab, "provider_call", download)
    assert colab.recover_diagnostics(receipt) == ("bootstrap.log",)
    assert (folder / "bootstrap.log").read_text() == "previous-complete-log"
    assert (folder / "comfy.log").read_text() == "downloaded"
    assert (folder / "environment.txt").read_text() == "downloaded"
    assert not list(folder.glob("*.tmp"))
