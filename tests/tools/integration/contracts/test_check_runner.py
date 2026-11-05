"""Verify runner contracts within the declared test scope."""

import json
import sys

import pytest

from tools.contracts import export as contracts


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
