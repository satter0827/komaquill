"""Serve the real local application against a loopback ComfyUI boundary for E2E."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.workspace import find_project
from komaquill.api import Client
from komaquill.domain.models import Session

from tests.backend.support.comfy import ComfyStub
from tests.backend.support.conversations import wire_reply
from tests.backend.support.storage import sqlite_connection

ROOT = Path(__file__).resolve().parents[3]


def scripted_completion(body: dict) -> dict:
    """Return declared fixture values, without claiming to understand arbitrary input."""
    schema = body["response_format"]["json_schema"]["schema"]["properties"]
    english = False
    context = {}
    for line in body["messages"][0]["content"].splitlines():
        try:
            context = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(context, dict) and "response_language" in context:
            english = context["response_language"] == "English"
            break
    if "response" in schema:
        value = {
            "current_state": {
                "relationship": "",
                "scenery": "Rain taps against the bookshop window."
                if english
                else "雨音が店内に響く。",
                "character": {
                    "clothing": None,
                    "pose": None,
                    "expression": None,
                    "held_objects": None,
                },
            },
            "action_narration": "",
            "requested_player_action": {
                "kind": "move",
                "destination": "Beach",
                "quote": "I head to the beach.",
            }
            if body["messages"][-1]["content"].split("\n")[0] == "I head to the beach."
            else None,
            "text": "A scripted reply for browser verification."
            if english
            else "画面検証用の決まった返答です。",
        }
        if body["messages"][-1]["content"] == "Is the imaginary compass here?":
            value["current_state"]["character"]["held_objects"] = [
                {"id": "imaginary-compass", "name": "Imaginary compass"}
            ]
            value["action_narration"] = (
                "Takes the compass." if english else "方位磁針を手に取ります。"
            )
        if body["messages"][-1]["content"] == "Please summarize our preparation.":
            value["text"] += "}}]}{"
    elif "personality" in schema:
        value = {
            "name": "Fixture character",
            "appearance": "An adult with short red hair.",
            "personality": "Calm",
            "age": "32",
            "gender": "woman",
        }
    elif "world" in schema:
        value = {
            "name": "Fixture story",
            "opening": "A visitor enters the bookshop.",
            "character_id": None,
        }
    elif "location" in schema:
        value = {
            "location": "An old bookshop.",
            "scenery": "Empty aisles, wooden shelves and a counter.",
            "atmosphere": "Rain streaks the windows.",
        }
    elif "text" not in schema:
        value = {key: f"Fixture translated {key.replace('_', ' ')}" for key in schema}
    else:
        value = {"text": "An adult woman in a quiet bookshop, no lettering."}
        if "status" in schema:
            value["status"] = "translated"
    return {
        "id": "fixture-completion",
        "object": "chat.completion",
        "created": 0,
        "model": body["model"],
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        wire_reply(
                            value, reaction="response" in schema and "player_move" not in schema
                        ),
                        ensure_ascii=False,
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def main() -> None:
    target = Path(
        os.environ.get(
            "KOMAQUILL_WEB_WORKSPACE", str(ROOT / ".artifacts/tmp/web-e2e" / uuid4().hex)
        )
    ).resolve()
    if not target.is_relative_to(ROOT / ".artifacts"):
        raise ValueError("Browser fixtures require an isolated artifact workspace.")
    client = Client.initialize(target)
    workspace = find_project(client.workspace)
    if os.environ.get("KOMAQUILL_WEB_ARCHIVE") == "1":
        payload = {
            "definition": {"title": "The preserved bookshop"},
            "turns": [
                {"user": "I open the book.", "assistant": "This page is still here."},
                {"user": "", "assistant": "The next page turns."},
            ],
        }
        with sqlite_connection(workspace.data / "komaquill.sqlite3") as connection:
            connection.execute(
                "INSERT INTO archived_conversations (id, payload, reason) VALUES (?, ?, ?)",
                (str(uuid4()), json.dumps(payload), "legacy_state"),
            )
    session = Session(
        endpoint="http://127.0.0.1:8188",
        runtime_id=uuid4(),
        backend="external",
        settings=workspace.settings(),
    )
    stub = ComfyStub(session)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def dispatch(self) -> None:
            content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/v1/models":
                response = httpx.Response(
                    200,
                    json={
                        "data": [
                            {"id": client._execution.settings.llm_profiles["lmstudio-4b"].model}
                        ]
                    },
                )
            elif self.path == "/v1/chat/completions":
                response = httpx.Response(200, json=scripted_completion(json.loads(content)))
            else:
                response = self.comfy_response(content)
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                if name.lower() != "content-length":
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def comfy_response(self, content: bytes) -> httpx.Response:
            request = httpx.Request(
                self.command,
                f"http://127.0.0.1{self.path}",
                headers=dict(self.headers),
                content=content,
            )
            return stub.respond(request)

        do_GET = dispatch
        do_POST = dispatch

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    session.endpoint = f"http://127.0.0.1:{server.server_port}"
    with (target / "komaquill.toml").open("a", encoding="utf-8") as configuration:
        configuration.write(
            f'\n[llm_profiles.lmstudio-4b]\nbase_url = "{session.endpoint}/v1"\n'
            'token_counting = "approximate"\n'
        )
    atomic_json(workspace.session_file, session.model_dump(mode="json"))
    import komaquill

    package = Path(str(komaquill.__file__)).resolve()
    if package.is_relative_to(ROOT / "src"):
        raise RuntimeError("Browser acceptance requires the installed wheel.")
    if output := os.environ.get("KOMAQUILL_WEB_CASE_DIR"):
        atomic_json(
            Path(output) / "fixture.json",
            {
                "initial_preferences": client.library.preferences().model_dump(mode="json"),
                "workspace": str(target),
                "llm": "loopback-scripted",
                "images": "loopback-comfy-stub",
                "package": str(package),
            },
        )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    child = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "komaquill",
            "--project",
            str(target),
            "web",
            "--port",
            "0",
        ],
        stdin=subprocess.DEVNULL,
    )
    try:
        sys.stdin.readline()
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        server.shutdown()
        server.server_close()
        client.close()


if __name__ == "__main__":
    main()
