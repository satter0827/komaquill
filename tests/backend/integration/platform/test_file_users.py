"""Confirm real Windows file ownership without stopping unrelated processes."""

import subprocess
import sys

import pytest
from komaquill.adapters.file_users import file_users


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Restart Manager contract")
def test_windows_reports_only_processes_holding_the_target_file(tmp_path):
    used, free = tmp_path / "used.db", tmp_path / "free.db"
    used.touch()
    free.touch()
    child = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            "import os,sys; f=open(sys.argv[1],'rb'); "
            "print(os.getpid(),flush=True); sys.stdin.readline()",
            str(used),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        identity = int(child.stdout.readline().strip())
        assert identity in file_users([used])
        assert file_users([free]) == set()
    finally:
        child.communicate("stop\n", timeout=10)
    assert file_users([used]) == set()
