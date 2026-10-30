"""Verify ownership and transport failures at the official CLI boundary."""

import subprocess
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from komaquill.adapters import colab as module
from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.colab import Colab, Receipt
from komaquill.domain.errors import AllocationRefused, KomaquillError


@pytest.fixture
def control(project, monkeypatch):
    project.runtime.mkdir(parents=True)
    item = Colab(project)
    item.config = project.runtime / "official.json"
    from komaquill.domain.models import RuntimeObservation

    def provider_call(action, *args, **kwargs):
        saved = item.receipt()
        present = saved is not None and item.entry(saved) is not None
        return RuntimeObservation(
            state="running" if present else "stopped",
            endpoint_id="expected",
            account_sha256="a" * 64,
        )

    monkeypatch.setattr(item, "provider_call", provider_call)
    return item


@pytest.mark.parametrize("case", ["success", "error", "refused", "accelerator", "timeout"])
def test_control_call_handles_transport_failures_without_echoing_responses(
    control, monkeypatch, case
):
    calls = []

    class Process:
        returncode = 0 if case == "success" else 1

        def communicate(self, **kwargs):
            if case == "timeout":
                raise subprocess.TimeoutExpired("private-command", 1)
            return b"private-response", {
                "refused": b"Allocation refused",
                "accelerator": b"Backend rejected accelerator",
            }.get(case, b"private-error")

    process = Process()
    monkeypatch.setattr(control, "command", lambda *args: ["official", *args])
    monkeypatch.setattr(
        module.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)) or process
    )
    monkeypatch.setattr(module, "terminate", lambda *args: calls.append("terminated"))
    if case == "success":
        control.call("status", interactive=True)
    else:
        with pytest.raises(
            AllocationRefused if case in {"refused", "accelerator"} else KomaquillError
        ) as error:
            control.call("status")
        assert "private" not in str(error.value)
    assert calls[0][0] == ["official", "status"]
    if case == "timeout":
        assert calls[-1] == "terminated"


@pytest.mark.parametrize("installed", ["missing", "wrong"])
def test_cli_version_is_checked_before_command_execution(control, monkeypatch, installed):
    def version(_):
        if installed == "missing":
            raise module.PackageNotFoundError
        return "0.0.0"

    monkeypatch.setattr(module, "version", version)
    with pytest.raises(KomaquillError):
        control.command("status")


@pytest.mark.parametrize("case", ["created", "refused", "uncertain"])
def test_allocation_persists_ownership_and_settings_before_request(control, monkeypatch, case):
    calls = []

    def call(*args, **kwargs):
        calls.append(args)
        if args[0] == "new":
            saved = control.receipt()
            assert saved is not None and saved.settings == control.settings
            if case == "refused":
                raise AllocationRefused("refused")
            if case == "created":
                atomic_json(control.config, {saved.name: {"endpoint": "expected"}})

    monkeypatch.setattr(control, "call", call)
    if case == "created":
        receipt, created = control.acquire("revision")
        assert created and receipt.endpoint_id == "expected"
        assert control.acquire("revision") == (receipt, False)
        with pytest.raises(KomaquillError):
            control.acquire("different")
        control.config.unlink()
        with pytest.raises(KomaquillError):
            control.acquire("revision")
        assert sum(args[0] == "new" for args in calls) == 1
    else:
        with pytest.raises(KomaquillError):
            control.acquire("revision")
        assert control.receipt_file.exists() == (case == "uncertain")


def test_high_memory_is_sent_only_by_explicit_frozen_setting(control, monkeypatch):
    control.settings = control.settings.model_copy(update={"gpu": "A100", "high_ram": True})
    calls = []

    def call(*args, **kwargs):
        calls.append(args)
        if args[0] == "new":
            atomic_json(control.config, {control.receipt().name: {"endpoint": "expected"}})

    monkeypatch.setattr(control, "call", call)
    receipt, created = control.acquire("revision", expected_account="a" * 64)
    allocation = next(args for args in calls if args[0] == "new")
    assert created and "--high-mem" in allocation and "A100" in allocation
    assert receipt.settings.high_ram


def test_usage_account_mismatch_never_allocates(control, monkeypatch):
    calls = []
    monkeypatch.setattr(control, "call", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(KomaquillError):
        control.acquire("revision", expected_account="b" * 64)
    assert control.receipt() is None
    assert all(args[0] != "new" for args in calls)


def test_prepare_transfers_verified_snapshot_and_keeps_execution_attached(control, monkeypatch):
    from tests.support.models import stage_models

    stage_models(control.project)
    control.settings = control.project.settings()
    receipt = Receipt(
        name="owned",
        runtime_id=uuid4(),
        source_revision="revision",
        endpoint_id="expected",
        account_sha256="a" * 64,
    )
    monkeypatch.setattr(control, "entry", lambda _: {})
    atomic_json(control.receipt_file, receipt.model_dump(mode="json"))
    commands = []

    from komaquill.adapters import deployment as deployment

    monkeypatch.setattr(deployment, "source_revision", lambda: "revision")

    def uploaded(*args, **kwargs):
        assert not control.lock.is_locked
        commands.append(args)

    monkeypatch.setattr(control, "call", uploaded)
    process = SimpleNamespace()
    monkeypatch.setattr(
        control, "remote_models", lambda *args: commands.append(("ssh",)) or process
    )
    assert control.prepare(receipt) is process
    script = (control.project.runtime / "deployment/prepare.py").read_text()
    assert "hashlib.file_digest" in script and str(receipt.runtime_id) in script
    compile(script, "prepare.py", "exec")
    import tarfile

    with tarfile.open(control.project.runtime / "deployment/payload.tar") as archive:
        assert b"poll_interval = 0.001" in archive.extractfile("komaquill.toml").read()
    assert [command[0] for command in commands] == ["upload", "exec", "ssh"]
    import shutil

    remote = control.project.root / "remote"
    remote.mkdir()
    shutil.copyfile(
        control.project.runtime / "deployment/payload.tar", remote / "komaquill-runtime.tar"
    )
    incoming = remote / "komaquill-runtime.tar"
    incoming.write_bytes(b"interrupted or corrupted runtime archive")
    with pytest.raises(RuntimeError):
        exec(compile(script.replace("/content", remote.as_posix()), "prepare.py", "exec"), {})
    assert incoming.exists()
    assert not list(
        (remote / f"komaquill-{receipt.runtime_id}/.artifacts/models").rglob("*.safetensors")
    )
    monkeypatch.setattr(control, "entry", lambda _: None)
    with pytest.raises(KomaquillError):
        control.prepare(receipt)


@pytest.mark.parametrize("case", ["connected", "retry", "exit", "timeout", "missing_tools"])
def test_ssh_binds_loopback_and_cleans_up_failed_connections(control, monkeypatch, case):
    receipt = Receipt(
        name="owned",
        runtime_id=uuid4(),
        source_revision="revision",
        endpoint_id="expected",
        account_sha256="a" * 64,
    )
    monkeypatch.setattr(
        module.shutil, "which", lambda _: None if case == "missing_tools" else "tool"
    )
    monkeypatch.setattr(control, "entry", lambda _: {})
    monkeypatch.setattr(control, "command", lambda *args: ["official", *args])
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)
    process = SimpleNamespace(poll=lambda: 1 if case == "exit" else None)
    commands = []
    monkeypatch.setattr(
        module.subprocess, "Popen", lambda args, **kwargs: commands.append(args) or process
    )
    terminated = []
    monkeypatch.setattr(module, "terminate", lambda *args: terminated.append(args[0]))
    attempts = []

    def connection(*args, **kwargs):
        attempts.append(args)
        if case == "retry" and len(attempts) == 1:
            raise OSError("temporarily unavailable")
        return nullcontext()

    monkeypatch.setattr(module.socket, "create_connection", connection)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    if case == "timeout":
        ticks = iter([0, 1000])
        monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    if case in {"exit", "timeout", "missing_tools"}:
        with pytest.raises(KomaquillError):
            control.tunnel(receipt)
        assert bool(terminated) == (case != "missing_tools")
    else:
        assert control.tunnel(receipt) is process
        assert "127.0.0.1:18188:127.0.0.1:8188" in commands[0]
        assert "StrictHostKeyChecking=accept-new" in commands[0]
        assert "HostKeyAlias=expected" in commands[0]
        assert len(attempts) == (2 if case == "retry" else 1)


def test_port_conflict_is_reported_before_allocation(monkeypatch):
    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, *args):
            raise OSError("occupied")

    monkeypatch.setattr(module.socket, "socket", Socket)
    with pytest.raises(KomaquillError):
        module.check_port(18188)


@pytest.mark.parametrize("case", ["failure", "missing_subject"])
def test_account_preflight_creates_no_ownership_record(control, monkeypatch, case):
    from komaquill.domain.models import RuntimeObservation

    calls = []
    monkeypatch.setattr(control, "call", lambda *args, **kw: calls.append(args))

    def provider(*args):
        assert not control.receipt_file.exists()
        if case == "failure":
            raise KomaquillError(code="authentication_failed")
        return RuntimeObservation()

    monkeypatch.setattr(control, "provider_call", provider)
    with pytest.raises(KomaquillError):
        control.acquire("revision")
    assert not control.receipt_file.exists()
    assert all(args[0] != "new" for args in calls)
