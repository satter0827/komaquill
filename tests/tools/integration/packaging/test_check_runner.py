"""Verify runner contracts within the declared test scope."""

from tools.packaging import build as package


def test_package_copies_ui_into_staging_without_changing_source(tmp_path, monkeypatch):
    backend = tmp_path / "src/backend/src/komaquill"
    backend.mkdir(parents=True)
    (backend / "__init__.py").write_text("# source")
    frontend = tmp_path / ".artifacts/build/frontend"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<main>Built UI</main>")
    monkeypatch.setattr(package, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(package.subprocess, "run", lambda command, **kwargs: calls.append(command))
    package.main()
    stage = next((tmp_path / ".artifacts/build/packages").iterdir())
    assert (stage / "src/komaquill/resources/web/index.html").read_text() == "<main>Built UI</main>"
    assert not (backend / "resources").exists()
    assert "--out-dir" in calls[0]
