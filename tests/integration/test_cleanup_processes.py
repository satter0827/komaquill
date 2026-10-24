"""Protect a live symlink-based Python environment during workspace cleanup."""

import os
import subprocess
import venv
from pathlib import Path

import psutil
import pytest

from komaquill.infrastructure import cleanup


@pytest.mark.skipif(os.name == "nt", reason="Linux venv executable resolves outside the workspace")
def test_cleanup_retains_files_used_by_a_symlink_venv(project):
    environment = project.runtime / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    python = environment / "bin/python"
    outside = project.root.parent / (project.root.name + "-outside")
    outside.mkdir()
    cache = project.cache / "ruff/in-use"
    temporary = project.temporary / "pytest-live/in-use"
    for path in (cache, temporary):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep")
    process = subprocess.Popen(
        [str(python), "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=outside,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        assert not Path(psutil.Process(process.pid).exe()).is_relative_to(project.runtime)
        items, report, failed = cleanup.execute(project, False)
        assert not failed
        assert all(item.reason == "in_use" for item in items)
        assert cache.read_text() == temporary.read_text() == "keep"
    finally:
        process.communicate(input="", timeout=5)
