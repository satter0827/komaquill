"""Keep executable hook entrypoints aligned with the tool layout."""

import shlex
from pathlib import Path


def test_push_hook_calls_an_existing_security_module():
    root = Path(__file__).resolve().parents[4]
    lines = (root / ".githooks/pre-push").read_text(encoding="utf-8").splitlines()
    command = shlex.split(next(line for line in lines if line.startswith("exec ")))[1:]
    assert command[0] == "node" and (root / command[1]).is_file()
    module = command[command.index("-m") + 1]
    assert (root / (module.replace(".", "/") + ".py")).is_file()
    assert command[command.index("--scope") + 1] == "history"
