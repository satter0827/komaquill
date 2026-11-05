"""Reject unapproved LAN access, persist approval and revoke every protected route."""

import hashlib
import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from komaquill.api import Client, KomaquillError
from komaquill.interfaces.http.app import create_app
from sqlalchemy import text


def test_lan_approval_survives_restart_and_revocation_blocks_files(tmp_path):
    api = Client.initialize(tmp_path)
    (tmp_path / "komaquill.toml").write_text('[web]\norigin="https://komaquill.home"\n')
    app = create_app(api)
    with TestClient(app, base_url="https://komaquill.home", client=("192.168.1.50", 52310)) as lan:
        assert lan.get("/api/v1/settings").status_code == 403
        response = lan.get("/")
        assert response.status_code == 403
        cookie = lan.cookies.get("__Host-komaquill-device")
        assert cookie and "HttpOnly" in response.headers["set-cookie"]
        assert "Secure" in response.headers["set-cookie"]
        device = api.devices.list()[0]
        assert device["id"] in response.text
        assert cookie not in json.dumps(api.devices.list())
        assert api.devices.state(cookie) == "pending"
        with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 52311)) as local:
            assert local.post(f"/api/v1/devices/{device['id']}/approve").status_code == 204
        assert lan.get("/api/v1/settings").status_code == 200
        assert lan.get("/api/v1/devices").status_code == 403
        assert lan.delete(f"/api/v1/devices/{device['id']}").status_code == 403
        assert (
            lan.post(
                "/api/v1/availability", headers={"origin": "https://attacker.example"}, json={}
            ).status_code
            == 403
        )
        api.close()
        reopened = Client(tmp_path)
        assert reopened.devices.state(cookie) == "approved"
        reopened.devices.decide(UUID(device["id"]), approve=False)
        for route in (
            "/api/v1/access",
            "/api/v1/settings",
            "/api/v1/images/not-an-id/files/0",
            "/assets/app.js",
        ):
            assert lan.get(route).status_code == 403
        with api.devices.database.engine.connect() as connection:
            saved = connection.execute(text("SELECT token_sha256 FROM devices")).scalar_one()
        assert saved == hashlib.sha256(cookie.encode()).hexdigest()
        with reopened.devices.database.engine.connect() as connection:
            states = (
                connection.execute(text("SELECT state FROM device_events ORDER BY recorded_at"))
                .scalars()
                .all()
            )
        assert states == ["pending", "approved", "revoked"]


def test_pending_devices_are_bounded_and_cannot_be_reapproved(tmp_path):
    api = Client.initialize(tmp_path)
    (tmp_path / "komaquill.toml").write_text(
        '[web]\npending_limit=1\norigin="https://komaquill.home"\n'
    )
    identity, token = api.devices.enroll()
    with pytest.raises(KomaquillError, match="operation_busy"):
        api.devices.enroll()
    api.devices.decide(UUID(identity), approve=True)
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.devices.decide(UUID(identity), approve=True)
    assert api.devices.state(token) == "approved"
    assert api.devices.state(None) is None
    assert api.devices.state("x" * 129) is None
    with pytest.raises(KomaquillError, match="request_not_found"):
        api.devices.decide(UUID(int=0), approve=False)


def test_proxy_configuration_protects_comfy_and_uses_private_storage(tmp_path):
    api = Client.initialize(tmp_path)
    with pytest.raises(KomaquillError, match="invalid_configuration"):
        api.export_web_proxy()
    api = Client(tmp_path)
    (tmp_path / "komaquill.toml").write_text('[web]\norigin="https://komaquill.home"\n')
    config = api.export_web_proxy(port=9123).read_text()
    assert '"https://komaquill.home"' in config and "tls internal" in config
    assert "forward_auth 127.0.0.1:9123" in config and "uri /api/v1/access" in config
    assert "handle_path /comfy/*" in config and "admin off" in config
    assert "header_up -Connection" in config and "header_up -Upgrade" in config
    assert "stream_timeout 60s" in config and "skip_install_trust" in config
