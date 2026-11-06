"""Verify transport rejection and private response headers without model connections."""

import pytest
from fastapi.testclient import TestClient
from komaquill.api import Client
from komaquill.interfaces.http import app as http_app


def test_storage_failure_retains_safe_diagnostics(tmp_path, monkeypatch):
    import json

    from komaquill.api import KomaquillError

    api = Client.initialize(tmp_path)

    def fail():
        raise KomaquillError("private response body", code="storage_failed")

    monkeypatch.setattr(api.images, "list", fail)
    with TestClient(http_app.create_app(api), base_url="http://localhost") as web:
        response = web.get("/api/v1/images")
    assert response.status_code == 400 and response.json()["code"] == "storage_failed"
    path = next((tmp_path / ".artifacts/records").rglob("cli.jsonl"))
    text = path.read_text(encoding="utf-8")
    assert "private response body" not in text
    failures = [json.loads(line)["failure"] for line in text.splitlines()]
    assert any(value and value["code"] == "storage_failed" for value in failures)


def test_private_headers_cover_success_origin_failure_and_unknown_hosts(tmp_path):
    with TestClient(
        http_app.create_app(Client.initialize(tmp_path)), base_url="http://localhost"
    ) as web:
        success = web.get("/api/v1/settings")
        origin = web.get("/api/v1/settings", headers={"origin": "https://external.example"})
        host = web.get("/api/v1/settings", headers={"host": "external.example"})
    assert (success.status_code, origin.status_code, host.status_code) == (200, 403, 400)
    for response in (success, origin, host):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("declared", [None, "8", "4096"])
def test_oversized_bodies_are_rejected_before_validation(tmp_path, declared):
    api = Client.initialize(tmp_path)
    (tmp_path / "komaquill.toml").write_text("[web]\nmax_body_bytes=1024\n", encoding="utf-8")
    headers = {"content-type": "application/json"}
    if declared is not None:
        headers["content-length"] = declared
    with TestClient(http_app.create_app(api), base_url="http://localhost") as web:
        response = web.post(
            "/api/v1/availability", content=iter([b"x" * 700, b"x" * 700]), headers=headers
        )
    assert response.status_code == 413
    assert response.headers["x-content-type-options"] == "nosniff"
