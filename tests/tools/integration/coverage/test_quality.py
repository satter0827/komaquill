"""Verify quality contracts within the declared test scope."""

import json

import pytest

from tools.checks import coverage as check_coverage


@pytest.mark.parametrize("case", ["pass", "threshold", "inventory"])
def test_coverage_gate_checks_source_inventory_and_configured_policy(tmp_path, monkeypatch, case):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src/backend/src").mkdir(parents=True)
    (tmp_path / "src/backend/src/app.py").write_text("pass")
    (tmp_path / "scripts/gate.py").write_text("pass")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.komaquill.coverage]\nline_minimum=90\nbranch_minimum=80"
    )
    folder = tmp_path / ".artifacts/records/reports/quality"
    folder.mkdir(parents=True)
    totals = {
        "covered_lines": 90 if case != "threshold" else 89,
        "num_statements": 100,
        "covered_branches": 80,
        "num_branches": 100,
    }
    files = {"src/backend/src/app.py": {}, "scripts/gate.py": {}}
    if case == "inventory":
        files["tests/test_app.py"] = {}
    (folder / "coverage.json").write_text(json.dumps({"files": files, "totals": totals}))
    monkeypatch.setattr(check_coverage, "__file__", str(tmp_path / "tools/checks/coverage.py"))
    if case == "pass":
        check_coverage.main()
    else:
        with pytest.raises(SystemExit):
            check_coverage.main()
