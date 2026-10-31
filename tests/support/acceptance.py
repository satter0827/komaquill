"""Exercise an installed wheel outside the checkout through standard HTTP APIs."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import traceback
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from PIL import Image

from tests.support.comfy import batch_count, multipart_input


def failure_snapshot(workspace: Path) -> dict:
    """Retain allowlisted failure facts before temporary acceptance data is removed."""
    state = workspace / ".artifacts/state"
    result = {}
    failure_fields = ("code", "stage", "exception_type", "http_status", "retryable")
    for name, fields in {
        "monitor.json": ("phase", "failure"),
        "stop.json": ("force",),
    }.items():
        try:
            value = json.loads((state / name).read_bytes())
            if not isinstance(value, dict):
                result[name] = {"invalid_state_type": type(value).__name__}
                continue
            result[name] = {field: value.get(field) for field in fields}
            if isinstance(result[name].get("failure"), dict):
                result[name]["failure"] = {
                    key: result[name]["failure"].get(key) for key in failure_fields
                }
            elif "failure" in result[name]:
                result[name]["failure"] = None
        except (OSError, ValueError) as error:
            result[name] = {"unreadable_type": type(error).__name__}
    failures = []
    for path in (workspace / ".artifacts/records/logs").rglob("*.jsonl"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if isinstance(event, dict) and isinstance(event.get("failure"), dict):
                    failures.append(
                        {
                            **{
                                key: event.get(key)
                                for key in ("created_at", "operation_id", "runtime_id")
                            },
                            "operation": event.get("operation"),
                            "result": event.get("result"),
                            "failure": {key: event["failure"].get(key) for key in failure_fields},
                        }
                    )
        except (OSError, ValueError) as error:
            failures.append({"unreadable_type": type(error).__name__})
    failures.sort(key=lambda row: row.get("created_at") or "")
    result["first_failure"] = next((row for row in failures if "failure" in row), None)
    result["failures"] = failures[-12:]
    return result


@contextlib.contextmanager
def acceptance_workspace(parent: Path):
    """Persist failure evidence before the isolated workspace is removed."""
    with tempfile.TemporaryDirectory(prefix="komaquill-wheel-", dir=parent) as temporary:
        root = Path(temporary)
        try:
            yield root
        except BaseException as error:
            destination = Path(
                os.environ.get(
                    "KOMAQUILL_ACCEPTANCE_REPORT_DIR",
                    str(
                        Path(__file__).resolve().parents[2]
                        / ".artifacts/records/reports/acceptance"
                    ),
                )
            )
            try:
                destination.mkdir(parents=True, exist_ok=True)
                frame = traceback.extract_tb(error.__traceback__)[-1]
                payload = {
                    "origin": {
                        "file": Path(frame.filename).name,
                        "function": frame.name,
                        "line": frame.lineno,
                    },
                    "exception_type": type(error).__name__,
                    "snapshot": failure_snapshot(root / "work"),
                }
                path = destination / (str(uuid4()) + ".json")
                path.write_text(json.dumps(payload), encoding="utf-8")
                error.add_note("Acceptance failure evidence: " + str(path))
            except (OSError, ValueError) as diagnostic:
                error.add_note(
                    "Acceptance evidence could not be saved: " + type(diagnostic).__name__
                )
            raise


def run_acceptance(wheel: Path | None = None) -> None:
    """Run one API acceptance scenario against local or isolated installation."""
    if wheel is not None:
        wheel = wheel.resolve()
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
    uploaded = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            route = self.path.split("?", 1)[0]
            if route == "/view":
                query = parse_qs(urlsplit(self.path).query)
                content = (
                    uploaded[
                        "/".join(
                            part
                            for part in (query.get("subfolder", [""])[0], query["filename"][0])
                            if part
                        )
                    ]
                    if query.get("type") == ["input"]
                    else png.getvalue()
                )
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
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/upload/image":
                name, folder, content = multipart_input(self.headers["Content-Type"], body)
                uploaded["/".join(part for part in (folder, name) if part)] = content
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    json.dumps({"name": name, "subfolder": folder, "type": "input"}).encode()
                )
                return
            data = json.loads(body)
            identity = str(uuid4())
            history[identity] = {
                "prompt": [0, identity, data["prompt"], data["extra_data"], ["9"]],
                "outputs": {
                    "9": {
                        "images": [
                            {"filename": f"result-{index}.png", "subfolder": "", "type": "output"}
                            for index in range(batch_count(data["prompt"]))
                        ]
                    }
                },
                "status": {"status_str": "success", "completed": True},
            }
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"prompt_id": identity}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    parent = Path(os.environ["LOCALAPPDATA"]) / "Temp" if os.name == "nt" else Path("/tmp")
    try:
        with acceptance_workspace(parent) as root:
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("KOMAQUILL_", "LANGCHAIN_", "LANGSMITH_"))
                and key not in {"PYTHONPATH", "PYTEST_ADDOPTS", "COVERAGE_PROCESS_START"}
            }
            env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
            python = Path(sys.executable)
            if wheel is not None:
                python = root / (
                    "venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python"
                )
                subprocess.run(
                    [uv, "venv", "--python", sys.executable, str(root / "venv")],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    [uv, "pip", "install", "--python", str(python), str(wheel)],
                    check=True,
                    capture_output=True,
                )
            else:
                env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src/backend/src")
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
                    timeout=60,
                )
                assert result.returncode == 0, (
                    result.stdout + result.stderr + json.dumps(failure_snapshot(workspace))
                )
                return result.stdout

            command("init")
            imported = subprocess.run(
                [
                    str(python),
                    "-B",
                    "-c",
                    "import sys; from komaquill.api import Client; "
                    "assert not any(name.startswith(('langchain', 'langsmith', 'fastapi', "
                    "'uvicorn', 'colab_cli')) for name in sys.modules)",
                ],
                cwd=workspace,
                env=env,
                capture_output=True,
                timeout=30,
            )
            assert imported.returncode == 0 and imported.stdout == imported.stderr == b""
            if wheel is not None:
                with zipfile.ZipFile(wheel) as archive:
                    assert "komaquill/resources/web/index.html" in archive.namelist()
                subprocess.run(
                    [
                        uv,
                        "pip",
                        "install",
                        "--python",
                        str(python),
                        f"komaquill[llm,web] @ {wheel.as_uri()}",
                    ],
                    check=True,
                    capture_output=True,
                )
            (workspace / "komaquill.toml").write_text(
                'poll_interval=0.01\nshutdown_timeout=2\nmodels=["klein-4b"]\n', encoding="utf-8"
            )
            conversation = json.loads(command("chat", "create", "Acceptance", "--profile", "fake"))
            sent = command("chat", "send", conversation["id"], "Hello")
            assert json.loads(sent.splitlines()[0])["state"] == "complete"
            reopened = json.loads(command("chat", "get", conversation["id"]))
            assert len(reopened["turns"]) == 1 and reopened["state"] == {"sample": "greeted"}
            command("chat", "send", conversation["id"], "Hello")
            assert json.loads(command("chat", "status", conversation["id"]))["revision"] == 2
            command(
                "chat",
                "command",
                conversation["id"],
                "set_state",
                "--expected-revision",
                "2",
                "--state",
                '{"place":"garden"}',
            )
            assert json.loads(command("chat", "resume", conversation["id"]))["state"] == {
                "place": "garden"
            }
            if wheel is not None:
                result = subprocess.run(
                    [
                        str(python),
                        "-B",
                        "-c",
                        textwrap.dedent("""
                            from pathlib import Path
                            from uuid import uuid4
                            from fastapi.testclient import TestClient
                            from komaquill.api import Client
                            from komaquill.interfaces.http.app import create_app
                            with TestClient(create_app(Client(Path.cwd()))) as http:
                                assert '<html' in http.get('/').text
                                assert len(http.get('/api/v1/conversations').json()) == 1
                                prefs = http.get('/api/v1/preferences').json()
                                assert prefs['locale'] == 'ja'
                                for collection in ('characters', 'stories'):
                                    url = '/api/v1/' + collection
                                    saved = http.post(url, json={'name': 'Created'}).json()
                                    item = url + '/' + saved['id']
                                    assert http.get(item).json()['name'] == 'Created'
                                    updated = http.put(item + '?expected_revision=0',
                                        json={'name': 'Updated'})
                                    assert updated.status_code == 200
                                    assert http.post(item + '/duplicate').status_code == 201
                                    deleted = http.delete(item + '?expected_revision=1')
                                    assert deleted.status_code == 204
                                prefs['locale'] = 'en'
                                changed = http.put('/api/v1/preferences', json=prefs)
                                assert changed.status_code == 200
                                assert http.get('/api/v1/preferences').json()['locale'] == 'en'
                                player = http.get('/api/v1/player').json()
                                selection = {'player_revision': player['revision']}
                                for kind, collection in [('character', 'characters'),
                                        ('story', 'stories')]:
                                    values = http.get('/api/v1/' + collection).json()
                                    value = next(v for v in values if v['sample_locale'] == 'ja')
                                    selection[kind] = {'id': value['id'],
                                        'revision': value['revision']}
                                result = http.post('/api/v1/conversations/from-presets',
                                    json={**selection, 'locale': 'ja'}).json()
                                assert result['definition']['presets']['character']['id'] == (
                                    selection['character']['id'])
                                result = http.post('/api/v1/conversations',
                                    json={'title':'Installed', 'locale':'en',
                                        'llm_profile':'fake'}).json()
                                identity = result['id']
                                turn_url = '/api/v1/conversations/' + identity + '/turns'
                                operation = http.post(turn_url,
                                    json={'operation_id': str(uuid4()), 'text': 'Hello'}).json()
                                saved = http.get('/api/v1/operations/' + operation['id']).json()
                                assert saved['state'] == 'complete'
                                restored = http.get('/api/v1/conversations/' + identity).json()
                                assert len(restored['turns']) == 1

                        """),
                    ],
                    cwd=workspace,
                    env=env,
                    capture_output=True,
                    timeout=30,
                )
                assert result.returncode == 0, result.stderr
            try:
                command(
                    "start",
                    "--no-open",
                    "--backend",
                    "external",
                    "--endpoint",
                    f"http://127.0.0.1:{server.server_port}",
                )
                command("generate", "--prompt", "a quiet blue lake", "--seed", "5")
                command("collect")
                script = (
                    "from komaquill.api import Client; from pathlib import Path; "
                    "c=Client(Path.cwd()); runs=c.images.list(); "
                    "assert len(runs)==1 and runs[0].state=='saved'; "
                    "assert c.artifact_path(f'runs/{runs[0].run_id}/"
                    "{runs[0].images[0]}').is_file()"
                )
                result = subprocess.run(
                    [str(python), "-B", "-c", script],
                    cwd=workspace,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, result.stderr
                command("stop")
                assert server.fileno() != -1
            finally:
                stopped = subprocess.run(
                    [str(python), "-B", "-m", "komaquill", "stop", "--force"],
                    cwd=workspace,
                    env=env,
                    capture_output=True,
                    timeout=30,
                )
                assert stopped.returncode == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
