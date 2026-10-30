"""Serve the real local application against a loopback ComfyUI boundary for E2E."""

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


def main() -> None:
    client = Client.initialize(ROOT / ".artifacts/tmp/web-e2e" / uuid4().hex)
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
            request = httpx.Request(
                self.command,
                f"http://127.0.0.1{self.path}",
                headers=dict(self.headers),
                content=content,
            )
            response = stub.respond(request)
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                if name.lower() != "content-length":
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = dispatch
        do_POST = dispatch

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    session.endpoint = f"http://127.0.0.1:{server.server_port}"
    atomic_json(workspace.session_file, session.model_dump(mode="json"))
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
