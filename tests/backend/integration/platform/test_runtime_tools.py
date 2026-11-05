"""Verify local bootstrap acquisition before the remote allocation boundary."""

import hashlib
from contextlib import contextmanager

import httpx
import pytest
from komaquill.adapters import runtime_tools
from komaquill.adapters.workspace import initialize
from komaquill.api import KomaquillError


@pytest.mark.parametrize("case", ["success", "corrupt_cache", "checksum", "http", "oversized"])
def test_tool_cache_is_verified_and_failed_downloads_are_not_published(tmp_path, monkeypatch, case):
    content = b"verified archive"
    expected = hashlib.sha256(content).hexdigest()
    resources = tmp_path / "package/resources/runtime/versions.toml"
    resources.parent.mkdir(parents=True)
    resources.write_text(f'uv_version="fixed"\nuv_linux_sha256="{expected}"\n')
    monkeypatch.setattr(runtime_tools, "files", lambda _: tmp_path / "package")
    project = initialize(tmp_path / "workspace")
    target = project.cache / "runtime-tools" / (expected + ".tar.gz")
    if case == "corrupt_cache":
        target.parent.mkdir(parents=True)
        target.write_bytes(b"broken")
    calls = []

    @contextmanager
    def stream(method, url, **kwargs):
        calls.append(url)
        assert url.endswith("/fixed/uv-x86_64-unknown-linux-gnu.tar.gz")
        yield httpx.Response(
            504 if case == "http" else 200, content=b"invalid" if case == "checksum" else content
        )

    monkeypatch.setattr(runtime_tools.httpx, "stream", stream)
    if case == "oversized":
        monkeypatch.setattr(runtime_tools, "MAX_ARCHIVE_BYTES", 1)
    if case in {"checksum", "http", "oversized"}:
        with pytest.raises(KomaquillError):
            runtime_tools.runtime_archive(project)
        assert not target.exists()
    else:
        assert runtime_tools.runtime_archive(project) == content
        assert runtime_tools.runtime_archive(project) == content
        assert len(calls) == 1 and target.read_bytes() == content
