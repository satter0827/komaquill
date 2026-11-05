"""Verify quality contracts within the declared test scope."""

import io
import json
import tarfile
import zipfile

import pytest

from tools.checks import distribution as check_package


@pytest.mark.parametrize(
    "case",
    [
        "pass",
        "installed",
        "tag",
        "wheel_version",
        "resources",
        "sdist_version",
        "missing_metadata",
        "artifacts",
        "editor_settings",
    ],
)
def test_distribution_gate_checks_real_archives(tmp_path, monkeypatch, case):
    expected = "1.2.3"
    (tmp_path / "src/backend").mkdir(parents=True)
    (tmp_path / "src/backend/pyproject.toml").write_text('[project]\nversion="1.2.3"')
    folder = tmp_path / ".artifacts/dist"
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / f"komaquill-{expected}-py3-none-any.whl", "w") as archive:
        archive.writestr(
            f"komaquill-{expected}.dist-info/METADATA",
            "Version: " + ("0.0.0" if case == "wheel_version" else expected),
        )
        if case == "editor_settings":
            archive.writestr(".vscode/settings.json", "{}")
        from komaquill.adapters.provenance import package_contents

        for name, content in package_contents():
            if case != "resources" or not name.endswith("/web/extension.js"):
                archive.writestr(name, content)
        if case == "artifacts":
            archive.writestr(".artifacts/private.txt", "private")
    with tarfile.open(folder / f"komaquill-{expected}.tar.gz", "w:gz") as archive:
        if case != "missing_metadata":
            data = ("Version: " + ("0.0.0" if case == "sdist_version" else expected)).encode()
            member = tarfile.TarInfo(f"komaquill-{expected}/PKG-INFO")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    monkeypatch.setattr(check_package, "__file__", str(tmp_path / "tools/checks/distribution.py"))
    monkeypatch.setattr(
        check_package, "version", lambda _: "0.0.0" if case == "installed" else expected
    )
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag" if case == "tag" else "branch")
    monkeypatch.setenv("GITHUB_REF_NAME", "v0.0.0")
    if case == "pass":
        check_package.main()
    else:
        with pytest.raises(SystemExit):
            check_package.main()


@pytest.mark.parametrize("valid", [False, True])
def test_e2e_gate_requires_one_wheel_and_clears_live_flags(tmp_path, monkeypatch, valid):
    from tools.checks import installed_wheel as check_e2e

    wheel = tmp_path / "dist"
    wheel.mkdir()
    if valid:
        (wheel / "komaquill-1.0-py3-none-any.whl").write_bytes(b"fixture")
    monkeypatch.setenv("KOMAQUILL_LIVE", "1")
    monkeypatch.setenv("KOMAQUILL_LIVE_STOP", "normal")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    calls = []
    monkeypatch.setattr(check_e2e, "run_owned", lambda command, **kw: calls.append(kw) or 0)
    monkeypatch.setattr(check_e2e, "input_fingerprint", lambda _: "fixture")
    monkeypatch.setattr(check_e2e, "source_revision", lambda _: "fixture")
    if valid:
        selected = wheel / "komaquill-1.0-py3-none-any.whl"
        reports = tmp_path / "reports"
        reports.mkdir()
        original = reports / "e2e.xml"
        original.write_text("Retained evidence")
        check_e2e.main(selected, reports)
        check_e2e.main(selected, reports)
        retained = [json.loads(p.read_bytes()) for p in reports.glob("*/run.json")]
        assert len(retained) == 2 and all(r["status"] == "passed" for r in retained)
        assert original.read_text() == "Retained evidence"
        assert calls[0]["env"]["KOMAQUILL_WHEEL"] == str(selected)
        assert "KOMAQUILL_LIVE" not in calls[0]["env"]
        assert "KOMAQUILL_LIVE_STOP" not in calls[0]["env"]
        assert "PYTEST_ADDOPTS" not in calls[0]["env"]
    else:
        with pytest.raises(SystemExit):
            check_e2e.main(wheel, tmp_path / "reports")


@pytest.mark.parametrize("interrupted", [False, True])
def test_installed_wheel_failure_evidence_is_retained(tmp_path, monkeypatch, interrupted):
    from tools.checks import installed_wheel as check_e2e

    wheel = tmp_path / "komaquill-1.0-py3-none-any.whl"
    wheel.write_bytes(b"fixture")
    monkeypatch.setattr(check_e2e, "input_fingerprint", lambda _: "fixture")
    monkeypatch.setattr(check_e2e, "source_revision", lambda _: "fixture")

    def fail(*_, **__):
        if interrupted:
            raise OSError("private details")
        raise check_e2e.subprocess.CalledProcessError(2, "fixture")

    monkeypatch.setattr(check_e2e, "run_owned", fail)
    with pytest.raises(OSError if interrupted else check_e2e.subprocess.CalledProcessError):
        check_e2e.main(wheel, tmp_path / "reports")
    record = next((tmp_path / "reports").glob("*/run.json")).read_text()
    result = json.loads(record)
    assert result["status"] == ("interrupted" if interrupted else "failed")
    assert result["exit_code"] == (None if interrupted else 2)
    assert result["wheel_sha256"] and result["finished_at"]
    assert "private details" not in record
