"""Verify check scope, interruption records and artifact-only packaging."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check, contracts, package


@pytest.mark.parametrize("source", ["path", "local", "explicit"])
def test_python_wrapper_uses_available_uv_without_overriding_explicit_setting(tmp_path, source):
    root = Path(__file__).resolve().parents[2]
    node = shutil.which("node")
    uv = shutil.which("uv") or str(root / ".artifacts/runtime/uv/uv.exe")
    if node is None or not Path(uv).is_file():
        pytest.skip("Node.js and uv are required for the wrapper integration check.")
    env = {**os.environ, "KOMAQUILL_PYTHON": sys.executable}
    env.pop("KOMAQUILL_UV", None)
    env["PATH"] = str(Path(uv).parent) + os.pathsep + env.get("PATH", "")
    expected = "uv"
    if source == "local":
        if sys.platform != "win32":
            pytest.skip("The workspace uv executable is used only on Windows.")
        local_uv = tmp_path / ".artifacts/runtime/uv/uv.exe"
        local_uv.parent.mkdir(parents=True)
        local_uv.touch()
        expected = str(local_uv)
    elif source == "explicit":
        expected = uv
        env["KOMAQUILL_UV"] = uv
    script = "import os; print(os.environ['KOMAQUILL_UV'])"
    if source != "local":
        script += (
            "; import subprocess; subprocess.run([os.environ['KOMAQUILL_UV'], '--version'], "
            "check=True, stdout=subprocess.DEVNULL)"
        )
    result = subprocess.run(
        [node, str(root / "scripts/python.mjs"), "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("scope", ["daily", "ci", "all"])
def test_check_plan_preserves_scope_without_running_tasks(tmp_path, monkeypatch, scope):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(
        check.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0),
    )
    assert check.main(scope, plan=True)
    assert ("--affected" in calls[0]) == (scope != "all")
    assert not (tmp_path / ".artifacts").exists()


@pytest.mark.parametrize("case", ["success", "failed", "missing", "interrupted", "changed"])
def test_check_records_failure_and_never_promotes_changed_inputs(tmp_path, monkeypatch, case):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check, "source_revision", lambda _: "fixture")
    values = iter(["before", "after" if case == "changed" else "before"])
    monkeypatch.setattr(check, "input_fingerprint", lambda _: next(values))
    monkeypatch.setenv("KOMAQUILL_ALLOW_REAL_LLM", "1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "not-forwarded")

    def run(command, **kwargs):
        assert "KOMAQUILL_ALLOW_REAL_LLM" not in kwargs["env"]
        assert "LANGSMITH_API_KEY" not in kwargs["env"]
        if case == "missing":
            raise FileNotFoundError
        if case == "interrupted":
            raise KeyboardInterrupt
        return SimpleNamespace(returncode=1 if case == "failed" else 0)

    monkeypatch.setattr(check.subprocess, "run", run)
    assert check.main("daily") is (case == "success")
    report = json.loads(next((tmp_path / ".artifacts").rglob("checks.json")).read_text())
    assert report["scope"] == "daily"
    assert report["status"] == (
        "passed"
        if case == "success"
        else "interrupted"
        if case in {"missing", "interrupted"}
        else "failed"
    )


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


def test_contract_export_checks_schema_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(contracts, "ROOT", tmp_path)
    (tmp_path / "src/contracts").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["contracts.py"])
    contracts.main()
    path = tmp_path / "src/contracts/openapi.json"
    schema = json.loads(path.read_text())
    assert "/api/v1/conversations" in schema["paths"]
    monkeypatch.setattr(sys, "argv", ["contracts.py", "--check"])
    contracts.main()
    path.write_text("{}")
    with pytest.raises(SystemExit):
        contracts.main()
