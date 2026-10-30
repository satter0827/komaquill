"""Protect a live symlink-based Python environment during workspace cleanup."""

import os
import subprocess
import venv
from pathlib import Path

import psutil
import pytest
from komaquill.adapters import cleanup


@pytest.mark.skipif(os.name == "nt", reason="Linux venv executable resolves outside the workspace")
@pytest.mark.parametrize("invocation", ["absolute", "path"])
def test_cleanup_retains_files_used_by_a_symlink_venv(project, invocation):
    environment_directory = project.runtime / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment_directory)
    python = environment_directory / "bin/python"
    outside = project.root.parent / (project.root.name + "-outside")
    outside.mkdir()
    cache = project.cache / "ruff/in-use"
    temporary = project.temporary / "pytest-live/in-use"
    for path in (cache, temporary):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep")
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    environment["PATH"] = str(python.parent) + os.pathsep + environment.get("PATH", "")
    executable = str(python) if invocation == "absolute" else "python"
    process = subprocess.Popen(
        [executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=outside,
        env=environment,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        assert not Path(psutil.Process(process.pid).exe()).is_relative_to(project.runtime)
        if invocation == "path":
            assert psutil.Process(process.pid).cmdline()[0] == "python"
        items, report, failed = cleanup.execute(project, False)
        assert not failed
        assert all(item.reason == "in_use" for item in items)
        assert cache.read_text() == temporary.read_text() == "keep"
    finally:
        process.communicate(input="", timeout=5)
