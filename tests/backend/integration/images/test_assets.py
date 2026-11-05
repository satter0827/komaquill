"""Verify images contracts within the declared test scope."""

from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest
from komaquill.adapters import asset_download as download
from komaquill.adapters import model_sources as sources
from komaquill.adapters.asset_store import model_path, register, verified_cache, verify_file
from komaquill.domain.errors import KomaquillError
from komaquill.domain.presets import ModelPreset

from tests.backend.support.assets import fake_asset as asset


@pytest.mark.parametrize("case", ["absent", "verified", "changed", "corrupt", "invalid"])
def test_verified_evidence_is_bound_to_bytes_and_file_identity(project, case):
    model = asset()
    path = model_path(project.models, model)
    if case == "absent":
        assert not verified_cache(project.models, model)
        assert not verify_file(project.models, model)
        return
    path.parent.mkdir(parents=True)
    path.write_bytes(b"model")
    register(project.models, model, path)
    assert verified_cache(project.models, model)
    if case == "changed":
        path.write_bytes(b"different")
    if case == "corrupt":
        (path.parent / "asset.json").write_text("{}")
    if case == "invalid":
        (path.parent / "asset.json").write_text("{broken")
    assert verified_cache(project.models, model) is (case == "verified")
    assert verify_file(project.models, model) is (case != "changed")


def test_equal_names_with_different_bytes_have_distinct_comfy_references(project):
    first, second = asset(b"first"), asset(b"second")
    assert first.filename == second.filename and first.reference != second.reference
    assert model_path(project.models, first) != model_path(project.models, second)


@pytest.mark.parametrize(
    "case",
    [
        "new",
        "cached",
        "existing",
        "resume",
        "ignored_range",
        "bad_range",
        "range_end",
        "hash",
        "http",
        "cancel",
        "mid_cancel",
        "disk",
    ],
)
def test_confirmed_downloads_publish_only_verified_bytes(project, monkeypatch, case):
    model = asset(size_bytes=5)
    target = model_path(project.models, model)
    target.parent.mkdir(parents=True)
    partial = target.with_suffix(".safetensors.part")
    if case in {"cached", "existing"}:
        target.write_bytes(b"model")
        if case == "cached":
            register(project.models, model, target)
    if case in {"resume", "ignored_range", "bad_range", "range_end"}:
        partial.write_bytes(b"mo")
    if case == "disk":
        monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    calls = []

    def stream(method, url, **kwargs):
        calls.append(kwargs)
        status = (
            206
            if case in {"resume", "bad_range"}
            else 416
            if case == "range_end"
            else 403
            if case == "http"
            else 200
        )
        return closing(
            httpx.Response(
                status,
                request=httpx.Request(method, url),
                headers={"Content-Range": "bytes 2-4/5" if case == "resume" else "invalid"},
                content=b"bad"
                if case == "hash"
                else b"del"
                if case in {"resume", "bad_range"}
                else b"model",
            )
        )

    monkeypatch.setattr(download.httpx, "stream", stream)
    cancelled = iter([False, True]) if case == "mid_cancel" else None

    def cancel():
        return next(cancelled) if cancelled else case == "cancel"

    if case in {"new", "cached", "existing", "resume", "ignored_range"}:
        assert download.download_asset(project.models, model, 1, cancel).read_bytes() == b"model"
        assert verified_cache(project.models, model)
        assert not partial.exists()
        assert bool(calls) is (case not in {"cached", "existing"})
    else:
        with pytest.raises(KomaquillError):
            download.download_asset(project.models, model, 1, cancel)
        assert not target.exists()
    if case == "resume":
        assert calls[0]["headers"]["Range"] == "bytes=2-"


@pytest.mark.parametrize(
    "case",
    [
        "hf",
        "civitai",
        "civitai_query",
        "nested",
        "http",
        "secret",
        "unknown",
        "unsafe",
        "bad_hf",
        "hash",
        "revision",
        "missing",
        "bad_civitai",
        "failure",
    ],
)
def test_only_public_immutable_sources_are_resolved(monkeypatch, case):
    sha = "b" * 64
    monkeypatch.setattr(
        sources,
        "get_hf_file_metadata",
        lambda *a, **k: SimpleNamespace(
            commit_hash="bad" if case == "revision" else "a" * 40,
            etag="bad" if case == "hash" else sha,
            size=None,
        ),
    )
    name = "model.pth" if case == "unsafe" else "model.safetensors"
    url = (
        "https://huggingface.co/owner/repo/resolve/main/"
        + ("sub/" if case == "nested" else "")
        + name
    )
    if case == "http":
        url = url.replace("https:", "http:")
    if case == "secret":
        url += "?token=private"
    if case == "unknown":
        url = "https://example.com/model.safetensors"
    if case == "bad_hf":
        url = "https://huggingface.co/bad"
    if case in {"civitai", "civitai_query", "missing", "bad_civitai", "failure"}:
        url = (
            "https://civitai.com/api/download/models/1"
            if case != "bad_civitai"
            else "https://civitai.com/models/1"
        )

        if case == "civitai_query":
            url += "?type=Model&format=SafeTensor"

        class Client:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def get(self, link):
                return httpx.Response(
                    500 if case == "failure" else 200,
                    request=httpx.Request("GET", link),
                    json={
                        "files": []
                        if case == "missing"
                        else [
                            {
                                "name": name,
                                "hashes": {"SHA256": sha},
                                "downloadUrl": url,
                                "sizeKB": 1,
                            }
                        ]
                    },
                )

        monkeypatch.setattr(sources.httpx, "Client", Client)
    if case in {"hf", "nested", "civitai", "civitai_query"}:
        result = sources.resolve_model(name, "checkpoints", url, 1)
        assert result.sha256 == sha
        assert "main" not in result.url
    else:
        with pytest.raises(KomaquillError):
            sources.resolve_model(name, "checkpoints", url, 1)


def test_model_inventory_tracks_verified_local_sources(project):
    from komaquill.adapters.asset_store import inventory

    local = asset().model_copy(update={"url": None, "local_only": True})
    path = model_path(project.models, local)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"model")
    register(project.models, local, path)
    assert inventory(project.models) == [local]
    assert download.download_asset(project.models, local, 1, lambda: False) == path
    path.unlink()
    with pytest.raises(KomaquillError):
        download.download_asset(project.models, local, 1, lambda: False)
    assert ModelPreset.model_validate(local.model_dump()).local_only
    with pytest.raises(ValueError):
        ModelPreset.model_validate({**local.model_dump(), "url": "https://example.com/model.sft"})
