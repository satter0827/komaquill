"""Verify runner contracts within the declared test scope."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("source", ["path", "local", "explicit"])
def test_python_wrapper_uses_available_uv_without_overriding_explicit_setting(tmp_path, source):
    root = Path(__file__).resolve().parents[4]
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
        [node, str(root / "tools/runtime/python.mjs"), "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == expected
