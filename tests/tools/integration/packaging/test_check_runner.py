"""Verify runner contracts within the declared test scope."""

from pathlib import Path
from subprocess import CalledProcessError

import pytest

from tools.packaging import build as package


@pytest.mark.parametrize("failed", [False, True])
def test_package_cleans_staging_after_success_or_failure(tmp_path, monkeypatch, failed):
    backend = tmp_path / "src/backend/src/komaquill"
    backend.mkdir(parents=True)
    (backend / "__init__.py").write_text("# source")
    frontend = tmp_path / ".artifacts/build/frontend"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<main>Built UI</main>")
    monkeypatch.setattr(package, "ROOT", tmp_path)
    stages = []

    def build(command, **_):
        stage = Path(command[command.index("--project") + 1])
        stages.append(stage)
        assert (
            stage / "src/komaquill/resources/web/index.html"
        ).read_text() == "<main>Built UI</main>"
        assert "--out-dir" in command
        if failed:
            raise CalledProcessError(2, command)

    monkeypatch.setattr(package.subprocess, "run", build)
    if failed:
        with pytest.raises(CalledProcessError):
            package.main()
    else:
        package.main()
    assert len(stages) == 1 and not stages[0].exists()
    assert not (backend / "resources").exists()
