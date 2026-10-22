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
