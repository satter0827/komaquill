"""Verify immutable publication, corruption detection and bounded imports."""

import hashlib

import pytest
from komaquill.adapters import object_store
from komaquill.adapters.object_store import COPY_BUFFER_BYTES, ObjectStore
from komaquill.domain.errors import KomaquillError


def test_duplicate_publication_reuses_verified_bytes(tmp_path):
    store = ObjectStore(tmp_path)
    first = store.publish(b"retained original")
    assert store.publish(b"retained original") == first
    assert store.read(first) == b"retained original"
    assert store.verify(first) == store.path(first)
    assert not list((tmp_path / "objects/.tmp").iterdir())
    assert store.publish(b"new content") != first


def test_import_hashes_the_stream_and_checks_the_expected_source(tmp_path, monkeypatch):
    source = tmp_path / "input.bin"
    content = b"x" * (COPY_BUFFER_BYTES + 19)
    source.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(type(source), "read_bytes", lambda _: pytest.fail("Use a bounded stream"))
    store = ObjectStore(tmp_path / "data")
    reference = store.import_file(source, expected_sha256=expected)
    assert reference.size_bytes == len(content)
    store.verify(reference)
    with pytest.raises(KomaquillError, match="identity_mismatch"):
        store.import_file(source, expected_sha256="0" * 64)


@pytest.mark.parametrize("corrupt", [b"changed bytes", b"bad"])
def test_corruption_is_reported_without_replacing_the_original_file(tmp_path, corrupt):
    store = ObjectStore(tmp_path)
    reference = store.publish(b"original data")
    store.path(reference).write_bytes(corrupt)
    for action in (lambda: store.publish(b"original data"), lambda: store.read(reference)):
        with pytest.raises(KomaquillError, match="identity_mismatch"):
            action()
    assert store.path(reference).read_bytes() == corrupt


def test_failed_publication_retains_existing_objects(tmp_path, monkeypatch):
    store = ObjectStore(tmp_path)
    retained = store.publish(b"first")
    monkeypatch.setattr(object_store, "replace_file", lambda *_: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        store.publish(b"second")
    assert store.read(retained) == b"first"
