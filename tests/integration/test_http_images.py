"""Check image, runtime and static-file HTTP contracts against local adapters."""

from contextlib import contextmanager
from uuid import uuid4

from fastapi.testclient import TestClient
from komaquill.adapters.atomic import atomic_json
from komaquill.api import Client, images, runtime
from komaquill.domain.status import SessionStatus
from komaquill.interfaces.http.app import create_app


def test_http_images_and_standard_views_share_the_public_api(project, session, client, monkeypatch):
    active = session.model_copy(update={"settings": project.settings(), "backend": "external"})
    atomic_json(project.session_file, active.model_dump(mode="json"))

    @contextmanager
    def connected(_):
        yield client, active

    monkeypatch.setattr(images, "connected", connected)
    monkeypatch.setattr(
        runtime.inspection, "inspect", lambda _: SessionStatus(gpu="active", connection="connected")
    )
    api = Client(project.root)
    with TestClient(create_app(api)) as http:
        identity = str(uuid4())
        request = {"request_id": identity, "prompt": "a green garden", "seed": 3}
        assert http.post("/api/v1/images", json=request).status_code == 202
        assert http.post("/api/v1/images", json=request).status_code == 202
        assert (
            http.post("/api/v1/images", json={**request, "prompt": "different"}).status_code == 409
        )
        assert http.get("/api/v1/images").json()[0]["run_id"] == identity
        assert http.post(f"/api/v1/images/{identity}/collect").json()["state"] == "saved"
        image = http.get(f"/api/v1/images/{identity}/files/0")
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        assert http.get(f"/api/v1/images/{identity}/files/-1").status_code == 404
        assert http.post("/api/v1/runtime/refresh").json()["connection"] == "connected"
        assert http.get("/api/v1/runtime").json()["observed_at"] is not None
        views = http.get("/api/v1/runtime/views").json()
        assert {row["view"] for row in views} == {"app", "graph"}
        graph = http.get("/api/v1/runtime/workflows/klein-4b/graph")
        assert graph.status_code == 200 and graph.json()["extra"]["linearMode"] is False


def test_http_static_cache_api_404_and_unknown_failures_are_safe(tmp_path, monkeypatch):
    client = Client.initialize(tmp_path / "work")
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<main>UI</main>")
    (ui / "assets/app.js").write_text("// bundled")
    with TestClient(create_app(client, ui_directory=ui)) as http:
        assert http.get("/").headers["cache-control"] == "no-cache"
        assert "immutable" in http.get("/assets/app.js").headers["cache-control"]
        assert http.get("/conversations/unknown").status_code == 200
        assert http.get("/api/v1/unknown").status_code == 404
        assert http.get("/api").status_code == 404
        assert http.get("/unknown.js").status_code == 404
        monkeypatch.setattr(
            client.conversations,
            "list",
            lambda: (_ for _ in ()).throw(RuntimeError("private wire payload")),
        )
        result = http.get("/api/v1/conversations")
        assert result.status_code == 500 and "private" not in result.text


def test_http_input_upload_preserves_bytes_and_rejects_paths(project):
    import io

    from PIL import Image

    source = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(source, "PNG")
    content = source.getvalue()
    api = Client(project.root)
    with TestClient(create_app(api)) as http:
        saved = http.post(
            "/api/v1/inputs?name=sample.png",
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert saved.status_code == 201, saved.text
        assert api.artifact_path(saved.json()["key"]).read_bytes() == content
        for name in ("../outside.png", "C:\\private.png"):
            assert (
                http.post(
                    "/api/v1/inputs",
                    params={"name": name},
                    content=content,
                    headers={"Content-Type": "application/octet-stream"},
                ).status_code
                == 400
            )
