"""Serve the real local application against a loopback ComfyUI boundary for E2E."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.workspace import find_project
from komaquill.api import Client
from komaquill.domain.models import Session
from komaquill.interfaces.http.app import create_app

from tests.support.comfy import ComfyStub

ROOT = Path(__file__).resolve().parents[2]


def scripted_completion(body: dict) -> dict:
    """Return declared fixture values, without claiming to understand arbitrary input."""
    schema = body["response_format"]["json_schema"]["schema"]["properties"]
    if "scene" in schema:
        english = '"response_language": "English"' in body["messages"][0]["content"]
        value = {
            "text": "A scripted reply for the browser fixture."
            if english
            else "画面検証用の決まった返答です。",
            "state": {"place": "Fixture bookshop"},
            "narration": "Rain taps against the bookshop window."
            if english
            else "雨音が古書店の窓をたたく。店主が本を取り出した。",
            "scene": None,
        }
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
    else:
        value = {"text": "An adult woman in a quiet bookshop, no lettering."}
    return {
        "id": "fixture-completion",
        "object": "chat.completion",
        "created": 0,
        "model": body["model"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)},
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
            if self.path == "/v1/chat/completions":
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
        configuration.write(f'\n[llm_profiles.lmstudio-4b]\nbase_url = "{session.endpoint}/v1"\n')
    atomic_json(workspace.session_file, session.model_dump(mode="json"))
    if output := os.environ.get("KOMAQUILL_WEB_EVIDENCE_DIR"):
        atomic_json(
            Path(output) / "fixture.json",
            {
                "initial_preferences": client.library.preferences().model_dump(mode="json"),
                "workspace": str(target),
                "llm": "loopback-scripted",
                "images": "loopback-comfy-stub",
            },
        )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        uvicorn.run(
            create_app(client, ui_directory=ROOT / ".artifacts/build/frontend"),
            host="127.0.0.1",
            port=8766,
            access_log=False,
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
