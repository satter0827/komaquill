"""Check image, runtime and static-file HTTP contracts against local adapters."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.colab import Colab, Receipt
from komaquill.adapters.configuration import Settings
from komaquill.api import Client, KomaquillError, images, runtime
from komaquill.domain.library.models import (
    CharacterInput,
    CreationRequest,
    ImagePromptDefaults,
    WaiPreferences,
)
from komaquill.domain.presets import WAI_PROFILE
from komaquill.domain.status import SessionStatus
from komaquill.interfaces.http.app import create_app

pytestmark = pytest.mark.usefixtures("scripted_services")


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
    with TestClient(create_app(api), base_url="http://localhost") as http:
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
        observed = http.get("/api/v1/runtime").json()
        assert observed["observed_at"] is not None
        assert observed["shutdown_target"] == "external"
        views = http.get("/api/v1/runtime/views").json()
        assert {row["view"] for row in views} == {"app", "graph"}
        graph = http.get("/api/v1/runtime/workflows/klein-4b/graph")
        assert graph.status_code == 200 and graph.json()["extra"]["linearMode"] is False


def test_wai_request_keeps_accepted_defaults_during_an_update(
    project, session, client, stub, monkeypatch
):
    active = session.model_copy(update={"settings": Settings(models=[WAI_PROFILE])})
    atomic_json(project.session_file, active.model_dump(mode="json"))

    @contextmanager
    def connected(_):
        yield client, active

    monkeypatch.setattr(images, "connected", connected)
    api = Client(project.root)
    preferences = api.library.preferences()
    saved = api.library.save_preferences(
        preferences.model_copy(
            update={
                "image_prompts": ImagePromptDefaults(
                    positive="diffuse light",
                    wai=WaiPreferences(seed=42, candidates=3, quality_enabled=False),
                )
            }
        )
    )

    def update_while_preparing(*_):
        api.library.save_preferences(
            saved.model_copy(update={"image_prompts": ImagePromptDefaults()})
        )

    monkeypatch.setattr(api.images, "_preflight", update_while_preparing)
    run_id = uuid4()
    run = api.images.submit(
        "Two people share an umbrella.",
        run_id=run_id,
        negative="crowd",
        use_prompt_preferences=True,
    )
    restarted = Client(project.root)
    repeated = restarted.images.submit(
        "Two people share an umbrella.",
        run_id=run_id,
        negative="crowd",
        use_prompt_preferences=True,
    )
    assert repeated == run
    assert stub.submissions == 1
    graph = next(iter(stub.history.values()))["prompt"][2]
    assert graph["3"]["inputs"]["seed"] == 42
    assert graph["5"]["inputs"]["batch_size"] == 3
    assert graph["7"]["inputs"]["text"] == "crowd"
    assert graph["10"]["inputs"]["string_a"] == ""
    assert graph["101"]["inputs"]["string_a"] == "diffuse light"


def test_image_rejects_runtime_settings_changed_during_preparation(
    project, session, client, stub, monkeypatch
):
    active = session.model_copy(update={"settings": Settings(models=[WAI_PROFILE])})
    atomic_json(project.session_file, active.model_dump(mode="json"))
    replacement = active.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connected(_):
        yield client, replacement

    api = Client(project.root)
    monkeypatch.setattr(images, "connected", connected)
    monkeypatch.setattr(
        api.images,
        "_preflight",
        lambda *_: atomic_json(project.session_file, replacement.model_dump(mode="json")),
    )
    with pytest.raises(KomaquillError, match="operation_conflict"):
        api.images.submit("A garden in the rain.")
    assert stub.submissions == 0 and api.images.list() == []


def test_portrait_uses_the_generation_snapshot_after_reconnecting(
    project, session, client, stub, monkeypatch
):
    from types import SimpleNamespace

    settings = Settings(models=[WAI_PROFILE])
    active = session.model_copy(update={"settings": settings})
    atomic_json(project.session_file, active.model_dump(mode="json"))
    api = Client(project.root)
    preferences = api.library.preferences()
    api.library.save_preferences(
        preferences.model_copy(
            update={"image_prompts": ImagePromptDefaults(wai=WaiPreferences(seed=42))}
        )
    )
    character = api.characters.create(CharacterInput(name="Fia", appearance="silver hair"))
    monkeypatch.setattr(api.library, "preflight", lambda *_: None)
    job, _ = api.library.accept(
        CreationRequest(
            operation_id=uuid4(),
            kind="portrait",
            target_id=character.id,
            expected_revision=character.revision,
            llm_profile="fake",
        )
    )
    accepted = job.image_generation
    assert accepted is not None
    assert "image_generation" not in job.model_dump(mode="json")

    replacement = Settings(models=["klein-4b", WAI_PROFILE])
    replacement = replacement.model_copy(
        update={
            "presets": {
                **replacement.presets,
                WAI_PROFILE: replacement.presets[WAI_PROFILE].configured_parameters({"steps": 55}),
            }
        }
    )
    active = active.model_copy(update={"settings": replacement})
    atomic_json(project.session_file, active.model_dump(mode="json"))

    @contextmanager
    def connected(_):
        yield client, active

    monkeypatch.setattr(images, "connected", connected)
    reopened = Client(project.root)
    monkeypatch.setattr(reopened.images, "_preflight", lambda *_: None)
    monkeypatch.setattr(
        reopened.library,
        "image_describer",
        SimpleNamespace(prepare_image=lambda *args, **kwargs: "A woman with silver hair."),
    )
    reopened.library.image_parameters["portrait"] = {"width": 1408, "height": 512}
    assert reopened.library.execute(job.id).state == "submitted"
    graph = next(iter(stub.history.values()))["prompt"][2]
    assert graph["3"]["inputs"]["seed"] == 42
    assert graph["3"]["inputs"]["steps"] == accepted.preset.steps
    assert graph["68"]["inputs"]["value"] == accepted.preset.width
    assert graph["69"]["inputs"]["value"] == accepted.preset.height


def test_http_runtime_keeps_owned_shutdown_without_an_endpoint(project, monkeypatch):
    receipt = Receipt(name="owned", runtime_id=uuid4(), settings=project.settings())
    atomic_json(project.colab_receipt_file, receipt.model_dump(mode="json"))

    def unavailable(_, owned):
        assert owned == receipt
        raise KomaquillError(code="connection_failed")

    monkeypatch.setattr(Colab, "refresh", unavailable)
    with TestClient(create_app(Client(project.root)), base_url="http://localhost") as http:
        assert http.get("/api/v1/runtime/views").json() == []
        cached = http.get("/api/v1/runtime")
        refreshed = http.post("/api/v1/runtime/refresh")
        for result in (cached, refreshed):
            assert result.status_code == 200
            assert result.json()["gpu"] == "unknown"
            assert result.json()["shutdown_target"] == "owned"
        assert refreshed.json()["connection"] == "not_started"

        cache = project.state / "runtime-observation.json"
        cache.write_text("{", encoding="utf-8")
        status = http.get("/api/v1/runtime").json()
        assert status["shutdown_target"] == "owned"
        assert status["unreadable"] == [cache.name]

        project.colab_receipt_file.unlink()
        assert http.get("/api/v1/runtime").json()["shutdown_target"] is None


def test_http_static_cache_api_404_and_unknown_failures_are_safe(tmp_path, monkeypatch):
    client = Client.initialize(tmp_path / "work")
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<main>UI</main>")
    (ui / "assets/app.js").write_text("// bundled")
    with TestClient(create_app(client, ui_directory=ui), base_url="http://localhost") as http:
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
    with TestClient(create_app(api), base_url="http://localhost") as http:
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
