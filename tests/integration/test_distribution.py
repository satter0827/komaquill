"""Verify workspace, distribution and worker payload boundaries."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tomllib
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from komaquill.infrastructure import deployment as deployment
from komaquill.infrastructure import provenance as provenance
from komaquill.infrastructure.configuration import Settings, initialize
from komaquill.interfaces import cli as cli


@pytest.mark.parametrize("existing", [False, True])
def test_initialize_preserves_settings_and_ignore_rules(tmp_path, existing):
    config = tmp_path / "komaquill.toml"
    ignore = tmp_path / ".gitignore"
    if existing:
        config.write_bytes(b'gpu = "L4"\n')
        ignore.write_bytes(b"private.txt")
    project = initialize(tmp_path)
    assert project.artifacts.is_dir() and project.state == project.artifacts / "state"
    assert project.session_file == project.state / "session.json"
    assert project.settings().gpu == ("L4" if existing else "T4")
    assert ignore.read_text().endswith("/.artifacts/\n")
    if existing:
        assert config.read_bytes() == b'gpu = "L4"\n'
        assert ignore.read_text().startswith("private.txt\n")
    before = (config.read_bytes(), ignore.read_bytes())
    initialize(tmp_path)
    assert before == (config.read_bytes(), ignore.read_bytes())


def test_cli_initializes_without_a_repository_and_reports_bad_config(tmp_path):
    result = CliRunner().invoke(cli.app, ["init", str(tmp_path)])
    assert result.exit_code == 0 and (tmp_path / "komaquill.toml").is_file()
    (tmp_path / "komaquill.toml").write_text("unknown = true\n")
    result = CliRunner().invoke(cli.app, ["init", str(tmp_path)])
    assert result.exit_code == 1 and "Traceback" not in result.output


def test_settings_precedence_and_startup_snapshot(project, monkeypatch):
    (project.root / "komaquill.toml").write_text('gpu="L4"\nlocal_port=19000\n')
    assert project.settings().gpu == "L4"
    monkeypatch.setenv("KOMAQUILL_GPU", "A100")
    assert project.settings().gpu == "A100"
    assert Settings(gpu="H100").gpu == "H100"
    snapshot = project.configured()
    monkeypatch.setenv("KOMAQUILL_GPU", "T4")
    (project.root / "komaquill.toml").write_text("broken = [")
    assert snapshot.settings().gpu == "A100" and snapshot.settings().local_port == 19000
    with pytest.raises(ValidationError):
        snapshot.settings().gpu = "L4"


def test_payload_is_reproducible_and_excludes_local_data(project, monkeypatch):
    monkeypatch.setattr(deployment, "source_revision", lambda: None)
    (project.root / "private.txt").write_text("not for transfer")
    project.runs.mkdir(parents=True)
    (project.runs / "private.txt").write_text("local work")
    first = deployment.create_payload(project)
    content = first.path.read_bytes()
    second = deployment.create_payload(project)
    assert content == second.path.read_bytes()
    assert first.sha256 == hashlib.sha256(content).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(content)) as archive:
        names = archive.getnames()
        assert "komaquill/gpu/bootstrap.py" in names
        assert "komaquill/resources/runtime/pylock.worker.toml" in names
        assert all(
            name.startswith("komaquill/") or name in {"manifest.json", "komaquill.toml"}
            for name in names
        )
        settings = tomllib.loads(archive.extractfile("komaquill.toml").read().decode())
        manifest = json.load(archive.extractfile("manifest.json"))
    assert manifest["settings"] == project.settings().model_dump(mode="json")
    assert settings["profiles"] == list(project.settings().profiles)
    assert manifest["code_sha256"] == first.code_sha256 and manifest["source_revision"] is None
    assert b"not for transfer" not in content and b"local work" not in content


def test_fingerprint_hashes_the_archived_snapshot_once(project, monkeypatch):
    entries = [("komaquill/gpu/worker.py", b"worker bytes")]
    monkeypatch.setattr(deployment, "gpu_contents", lambda: iter(entries))
    monkeypatch.setattr(deployment, "source_revision", lambda: "revision")
    payload = deployment.create_payload(project)
    assert payload.code_sha256 == provenance.code_sha256(entries)
    assert provenance.code_sha256(entries) != provenance.code_sha256(
        [("komaquill/gpu/worker.py", b"other")]
    )


@pytest.mark.parametrize("case", ["installed", "no_git", "success", "failure"])
def test_git_is_optional_and_never_reads_the_workspace_repository(tmp_path, monkeypatch, case):
    package = tmp_path / "src/komaquill"
    package.mkdir(parents=True)
    monkeypatch.setattr(provenance, "files", lambda _: package)
    if case != "installed":
        (tmp_path / ".git").mkdir()
    monkeypatch.setattr(provenance.shutil, "which", lambda _: None if case == "no_git" else "git")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1 if case == "failure" else 0, stdout="revision\n")

    monkeypatch.setattr(provenance.subprocess, "run", run)
    assert provenance.source_revision() == ("revision" if case == "success" else None)
    assert bool(calls) == (case in {"success", "failure"})
    if calls:
        assert calls[0][2] == str(tmp_path)


def test_payload_allowlist_and_fixed_versions():
    names = [name for name, _ in provenance.package_contents()]
    assert len(names) == len(set(names))
    assert all(
        name.endswith(".py")
        or name.startswith("komaquill/interfaces/comfyui/web/")
        or name.removeprefix("komaquill/resources/") in provenance.RESOURCE_NAMES
        for name in names
    )
    root = Path(__file__).resolve().parents[2]
    runtime = tomllib.loads(
        files("komaquill").joinpath("resources/runtime/versions.toml").read_text()
    )
    assert runtime["python_version"] == (root / ".python-version").read_text().strip()
    assert not list((root / "scripts").glob("worker*"))
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["coverage"]["run"]["source"] == ["src", "scripts", "bootstrap"]
    assert "*/tests/*" in config["tool"]["coverage"]["run"]["omit"]


@pytest.mark.skipif(os.name != "nt", reason="Windows bootstrap uses PowerShell")
def test_powershell_entrypoints_parse_and_create_only_project_artifacts(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "bootstrap").mkdir()
    shutil.copyfile(root / "bootstrap/_environment.ps1", tmp_path / "bootstrap/_environment.ps1")
    catalog = tmp_path / "src/komaquill/resources/messages.json"
    catalog.parent.mkdir(parents=True)
    shutil.copyfile(root / "src/komaquill/resources/messages.json", catalog)
    command = """
    $ErrorActionPreference = 'Stop'
    foreach ($folder in @('bootstrap', 'scripts')) {
      Get-ChildItem -LiteralPath (Join-Path $args[0] $folder) -Filter *.ps1 | ForEach-Object {
        $parseErrors = $null
        [System.Management.Automation.Language.Parser]::ParseFile(
          $_.FullName, [ref]$null, [ref]$parseErrors) | Out-Null
        if ($parseErrors.Count -ne 0) { throw 'PowerShell syntax is invalid.' }
      }
    }
    . (Join-Path $args[1] 'bootstrap/_environment.ps1')
    if ($env:TEMP -ne (Join-Path $args[1] '.artifacts/tmp')) { throw 'Unexpected temporary path.' }
    $expectedPython = Join-Path $args[1] '.artifacts/runtime/venv/Scripts/python.exe'
    if ($KomaquillPython -ne $expectedPython) { throw 'Unexpected Python path.' }
    """
    # PowerShell -Command binds remaining arguments to the script block.
    script = tmp_path / "verify.ps1"
    script.write_text(command, encoding="utf-8")
    subprocess.run(
        [shell, "-NoProfile", "-File", str(script), str(root), str(tmp_path)],
        check=True,
        capture_output=True,
    )


def test_worker_lock_records_exact_public_sources_and_hashes():
    from urllib.parse import urlsplit

    lock = tomllib.loads(
        files("komaquill").joinpath("resources/runtime/pylock.worker.toml").read_text()
    )
    assert lock["requires-python"] == "==3.12.*"
    packages = {item["name"]: item for item in lock["packages"]}
    for item in packages.values():
        for artifact in [*item.get("wheels", []), *([item["sdist"]] if "sdist" in item else [])]:
            url = urlsplit(artifact["url"])
            assert url.scheme == "https" and not url.username and not url.password and not url.query
            assert url.hostname in {
                "files.pythonhosted.org",
                "download.pytorch.org",
                "download-r2.pytorch.org",
            }
            assert len(artifact["hashes"]["sha256"]) == 64
    assert packages["triton"]["index"] == "https://pypi.org/simple"
    assert all("files.pythonhosted.org" in row["url"] for row in packages["triton"]["wheels"])
    assert all(
        urlsplit(row["url"]).hostname.endswith("pytorch.org") for row in packages["torch"]["wheels"]
    )


def test_worker_lock_satisfies_pinned_comfy_requirements():
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    root = Path(__file__).resolve().parents[2]
    requirements = root / "tests/fixtures/comfy-requirements.txt"
    lock = tomllib.loads(
        files("komaquill").joinpath("resources/runtime/pylock.worker.toml").read_text()
    )
    packages = {canonicalize_name(item["name"]): item["version"] for item in lock["packages"]}
    runtime = tomllib.loads(
        files("komaquill").joinpath("resources/runtime/versions.toml").read_text()
    )
    assert (
        "/blob/" + runtime["comfy_revision"] + "/requirements.txt"
        in requirements.read_text().splitlines()[0]
    )
    for line in requirements.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            requirement = Requirement(line)
            name = canonicalize_name(requirement.name)
            assert name in packages, name
            assert packages[name] in requirement.specifier, name
