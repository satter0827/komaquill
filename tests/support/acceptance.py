"""Exercise an installed wheel outside the checkout through standard HTTP APIs."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from PIL import Image


def run_acceptance(wheel: Path | None = None) -> None:
    """Run one API acceptance scenario against local or isolated installation."""
    uv = os.environ.get("KOMAQUILL_UV") or shutil.which("uv")
    if wheel is not None:
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
            for name in list(env):
                if name.startswith("KOMAQUILL_") and name != "KOMAQUILL_UV":
                    env.pop(name)
            for name in [
                "PYTHONPATH",
                "KOMAQUILL_PROJECT_DIR",
                "COVERAGE_PROCESS_START",
                "PYTEST_ADDOPTS",
            ]:
                env.pop(name, None)
            python = root / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
            if wheel is not None:
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
            else:
                python = Path(sys.executable)
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
            bundle = workspace / "presets/custom"
            bundle.mkdir(parents=True)
            for name in ("preset.toml", "workflow.json", "workflow-api.json"):
                if wheel is not None:
                    with zipfile.ZipFile(wheel) as archive:
                        content = archive.read("komaquill/resources/presets/pony/" + name)
                else:
                    content = (
                        files("komaquill").joinpath("resources/presets/pony/" + name).read_bytes()
                    )
                (bundle / name).write_bytes(content)
            (workspace / "komaquill.toml").write_text(
                'poll_interval=0.01\nshutdown_timeout=2\nprofiles=["custom"]\n'
                "[presets.custom]\nsteps=27\n"
            )
            command("--help")
            source_file = workspace / "story.md"
            source_file.write_bytes(
                files("komaquill")
                .joinpath("resources/stories/examples/rainy-cat/source.md")
                .read_bytes()
            )
            if wheel is not None:
                missing = subprocess.run(
                    [
                        str(python),
                        "-B",
                        "-m",
                        "komaquill",
                        "stories",
                        "plan",
                        str(source_file),
                        "--llm-profile",
                        "fake",
                    ],
                    cwd=workspace,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=30,
                )
                assert missing.returncode == 1 and "komaquill[llm]" in missing.stderr
                subprocess.run(
                    [
                        uv,
                        "pip",
                        "install",
                        "--python",
                        str(python),
                        f"komaquill[llm] @ {wheel.as_uri()}",
                    ],
                    env=env,
                    check=True,
                    capture_output=True,
                )
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
                    illustrated = json.loads(
                        command(
                            "stories",
                            "generate",
                            str(source_file),
                            "--images",
                            "4",
                            "--llm-profile",
                            "fake",
                            "--image-profile",
                            "custom",
                            "--json",
                        ).stdout
                    )
                    assert illustrated["complete"] and illustrated["image_count"] == 4
                    shown = json.loads(
                        command("stories", "show", illustrated["story_id"], "--json").stdout
                    )
                    assert shown["render_id"] == illustrated["render_id"]
                    resumed = json.loads(
                        command("stories", "resume", illustrated["render_id"], "--json").stdout
                    )
                    assert resumed["complete"]
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
            assert len(runs) == 5
            runs = sorted(runs, key=lambda path: json.loads(path.read_bytes())["created_at"])
            run = json.loads(runs[0].read_bytes())
            assert run["state"] == "saved"
            if wheel is not None:
                assert run["source_revision"] is None
            assert run["image_metadata"] and run["code_sha256"]
            graph = json.loads((runs[0].parent / "workflow-api.json").read_bytes())
            assert graph["3"]["inputs"]["steps"] == 27
            command(
                "evaluations",
                "add",
                run["run_id"],
                "--decision",
                "accept",
                "--criterion",
                "composition",
                "--reason",
                "Matches the scene.",
            )
            evaluation_data = json.loads(command("evaluations", "list").stdout)
            assert len(evaluation_data) == 1
            evidence = workspace / ".artifacts/records/reports/evidence.json"
            command("evaluations", "export", "--output", str(evidence))
            exported = json.loads(evidence.read_text(encoding="utf-8"))
            assert all(item["workflow_integrity"] is True for item in exported["runs"])
            assert all(item["images"][0]["matches_record"] is True for item in exported["runs"])
            assert exported["exported_at"] and exported["exporter"]["code_sha256"]
            assert sum(bool(item["story_context"]) for item in exported["runs"]) == 4
            command("evaluations", "import", str(evidence))
            command("clean", "--dry-run")
            assert runs[0].is_file()
            script = "\n".join(
                [
                    "import pathlib, tarfile, subprocess, sys",
                    "from komaquill.infrastructure.deployment import create_payload",
                    "from komaquill.infrastructure.configuration import find_project",
                    "from komaquill.infrastructure.provenance import source_revision",
                    "assert source_revision() is None" if wheel is not None else "pass",
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
