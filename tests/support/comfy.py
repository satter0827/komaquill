"""Provide ComfyUI test boundaries without network or GPU dependencies."""

import io
import json
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
from PIL import Image

from komaquill.domain.models import JsonObject, Session


@dataclass
class ComfyStub:
    session: Session
    history: JsonObject = field(default_factory=dict)
    submissions: int = 0
    image_requests: int = 0
    drop_response: bool = False
    image_fails: bool = False
    corrupt_image: bool = False
    invalid_images: set[str] = field(default_factory=set)
    queue: JsonObject = field(default_factory=lambda: {"queue_running": [], "queue_pending": []})

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/userdata/komaquill/session.json":
            return httpx.Response(200, json=self.session.model_dump(mode="json"))
        if path == "/history":
            return httpx.Response(200, json=self.history)
        if path == "/queue":
            return httpx.Response(200, json=self.queue)
        if path == "/prompt":
            self.submissions += 1
            data = json.loads(request.content)
            prompt_id = str(uuid4())
            self.history[prompt_id] = {
                "prompt": [0, prompt_id, data["prompt"], data["extra_data"], ["9"]],
                "outputs": {
                    "9": {
                        "images": [
                            {"filename": "../../outside.png", "subfolder": "", "type": "output"}
                        ]
                    }
                },
                "status": {"status_str": "success", "completed": True},
            }
            if self.drop_response:
                raise httpx.ReadTimeout("connection interrupted", request=request)
            return httpx.Response(200, json={"prompt_id": prompt_id})
        if path == "/view":
            self.image_requests += 1
            if self.image_fails:
                raise httpx.ReadError("interrupted", request=request)
            output = io.BytesIO()
            Image.new("RGB", (8, 8), "green").save(output, "PNG")
            return httpx.Response(
                200,
                content=b"not an image"
                if self.corrupt_image or request.url.params.get("filename") in self.invalid_images
                else output.getvalue(),
            )
        if path == "/system_stats":
            return httpx.Response(200, json={"devices": [{"type": "cuda"}]})
        return httpx.Response(404)
