"""Exercise an installed wheel outside the checkout through standard HTTP APIs."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image


@pytest.mark.skipif(
    not os.environ.get("KOMAQUILL_WHEEL"), reason="Set KOMAQUILL_WHEEL after building"
)
def test_installed_wheel_generates_and_prepares_payload_without_git():
    directory = Path(os.environ["KOMAQUILL_WHEEL"]).resolve()
    wheel = next(directory.glob("komaquill-*-py3-none-any.whl"))
    uv = os.environ.get("KOMAQUILL_UV") or shutil.which("uv")
    assert uv
    history = {
        "before-connection": {
            "prompt": [0, "before-connection", {}, {}, []],
            "status": {"completed": True},
            "outputs": {},
        }
    }
    png = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(png, "PNG")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            route = self.path.split("?", 1)[0]
            if route == "/view":
                content = png.getvalue()
            elif route == "/history":
                content = json.dumps(history).encode()
            elif route == "/queue":
                content = b'{"queue_running":[],"queue_pending":[]}'
            elif route == "/object_info":
                content = json.dumps(
                    {
                        "CheckpointLoaderSimple": {
                            "input": {"required": {"ckpt_name": [["v6.safetensors"]]}}
                        }
                    }
                ).encode()
            elif route == "/system_stats":
                content = b'{"devices":[{"type":"cuda"}]}'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            identity = str(uuid4())
            history[identity] = {
                "prompt": [0, identity, data["prompt"], data["extra_data"], ["9"]],
                "outputs": {
                    "9": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}
                },
                "status": {"status_str": "success", "completed": True},
            }
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"prompt_id": identity}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Temporary isolation outside the checkout is removed even after a failed assertion.
    parent = Path(os.environ["LOCALAPPDATA"]) / "Temp" if os.name == "nt" else Path("/tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="komaquill-wheel-", dir=parent) as temporary:
            root = Path(temporary)
            env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
            for name in [
                "PYTHONPATH",
                "KOMAQUILL_PROJECT_DIR",
                "COVERAGE_PROCESS_START",
                "PYTEST_ADDOPTS",
            ]:
                env.pop(name, None)
            python = root / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
            subprocess.run(
                [uv, "venv", "--python", "3.12", str(root / "venv")],
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [uv, "pip", "install", "--python", str(python), str(wheel)],
                env=env,
                check=True,
                capture_output=True,
            )
            workspace = root / "work"
            workspace.mkdir()

            def command(*args):
                result = subprocess.run(
                    [str(python), "-B", "-m", "komaquill", *args],
                    cwd=workspace,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=30,
                )
                assert result.returncode == 0, result.stdout + result.stderr
                return result

            command("init")
            (workspace / "komaquill.toml").write_text("poll_interval=0.01\nshutdown_timeout=2\n")
            command("--help")
            output = root / "monitor.txt"
            with output.open("w", encoding="utf-8") as handle:
                monitor = subprocess.Popen(
                    [
                        str(python),
                        "-B",
                        "-m",
                        "komaquill",
                        "start",
                        "--no-open",
                        "--backend",
                        "external",
                        "--endpoint",
                        f"http://127.0.0.1:{server.server_port}",
                    ],
                    cwd=workspace,
                    env=env,
                    stdout=handle,
                    stderr=handle,
                )
                try:
                    session = workspace / ".artifacts/state/session.json"
                    deadline = time.monotonic() + 30
                    while not session.is_file() and monitor.poll() is None:
                        assert time.monotonic() < deadline
                        time.sleep(0.05)
                    assert session.is_file(), output.read_text(encoding="utf-8")
                    assert monitor.wait(timeout=30) == 0
                    command("generate", "--prompt", "a quiet blue lake", "--seed", "5")
                    command("collect")
                    command("status")
                    for arguments, expected in [
                        (["open", "--view", "invalid"], 2),
                        (["doctor"], 1),
                    ]:
                        failed = subprocess.run(
                            [str(python), "-B", "-m", "komaquill", *arguments],
                            cwd=workspace,
                            env=env,
                            text=True,
                            encoding="utf-8",
                            capture_output=True,
                            timeout=30,
                        )
                        assert failed.returncode == expected
                        assert "Traceback" not in failed.stdout + failed.stderr
                    command("stop")
                    assert monitor.wait(timeout=30) == 0
                finally:
                    if monitor.poll() is None:
                        monitor.terminate()
                        monitor.wait(timeout=10)
            runs = list((workspace / ".artifacts/data/runs").glob("*/run.json"))
            assert len(runs) == 1
            run = json.loads(runs[0].read_bytes())
            assert run["state"] == "saved" and run["source_revision"] is None
            assert run["image_metadata"] and run["code_sha256"]
            script = "\n".join(
                [
                    "import pathlib, tarfile, subprocess, sys",
                    "from komaquill.infrastructure.deployment import create_payload",
                    "from komaquill.infrastructure.configuration import find_project",
                    "from komaquill.infrastructure.provenance import source_revision",
                    "assert source_revision() is None",
                    "p = create_payload(find_project())",
                    "assert p.path.is_file() and p.sha256",
                    "dest = pathlib.Path('payload'); dest.mkdir()",
                    "with tarfile.open(p.path) as archive:",
                    "    archive.extractall(dest, filter='data')",
                    "code = 'import sys; sys.path.insert(0, sys.argv[1]); '",
                    "code += 'from komaquill.gpu import bootstrap, worker'",
                    "args = [sys.executable, '-I', '-c', code, str(dest.resolve())]",
                    "subprocess.run(args, check=True)",
                ]
            )
            subprocess.run(
                [str(python), "-B", "-c", script],
                cwd=workspace,
                env=env,
                check=True,
                capture_output=True,
                timeout=30,
            )
            assert thread.is_alive()
            with contextlib.closing(
                __import__("urllib.request").request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/system_stats"
                )
            ) as response:
                assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
