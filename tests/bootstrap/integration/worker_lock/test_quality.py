"""Verify quality contracts within the declared test scope."""

from pathlib import Path

from tools.checks import worker_lock as check_worker_lock


def test_worker_lock_writer_documents_public_hash_exceptions(tmp_path, monkeypatch):
    resource = tmp_path / "src/backend/src/komaquill/resources/runtime/pylock.worker.toml"
    resource.parent.mkdir(parents=True)
    monkeypatch.setattr(
        check_worker_lock, "__file__", str(tmp_path / "tools/checks/worker_lock.py")
    )
    monkeypatch.setenv("KOMAQUILL_UV", "uv-test")

    def export(args, **kwargs):
        Path(args[args.index("--output-file") + 1]).write_text(
            'wheels = [{hashes = { sha256 = "public" }}]\n'
        )

    monkeypatch.setattr(check_worker_lock.subprocess, "run", export)
    from bootstrap import update_worker_lock

    monkeypatch.setattr(
        update_worker_lock, "__file__", str(tmp_path / "bootstrap/update_worker_lock.py")
    )
    update_worker_lock.main()
    text = resource.read_text()
    assert text.endswith("# pragma: allowlist secret Public artifact SHA256 from uv.lock.\n")
    check_worker_lock.main()
