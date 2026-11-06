"""Verify workflow exports and package structure."""

import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_import_has_no_filesystem_side_effects(tmp_path):
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(ROOT / ".artifacts/cache/pycache"),
    }
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import komaquill.interfaces.cli.main, komaquill.worker.worker; "
            "import komaquill.adapters.colab",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    assert not list(tmp_path.iterdir())


def test_module_entrypoint_and_metadata_agree():
    result = subprocess.run(
        [sys.executable, "-m", "komaquill", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == version("komaquill")
