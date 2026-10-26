"""Provide ComfyUI test boundaries without network or GPU dependencies."""

import io
import json
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from uuid import uuid4

import httpx
from PIL import Image

from komaquill.domain.models import JsonObject, Session


def multipart_input(content_type: str, content: bytes) -> tuple[str, str, bytes]:
    """Decode the standard image upload used by our CLI and wheel scenarios."""
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + content
    )
    values = {}
    filename = ""
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        values[name] = part.get_payload(decode=True)
        if name == "image":
            filename = part.get_filename()
    return filename, values["subfolder"].decode(), values["image"]


def batch_count(graph: JsonObject) -> int:
    for node in graph.values():
        if node["class_type"] == "EmptyFlux2LatentImage":
            return node["inputs"]["batch_size"]
        if node["class_type"] == "RepeatImageBatch":
            return node["inputs"]["amount"]
    return 1


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
    uploaded_inputs: dict[str, bytes] = field(default_factory=dict)
    upload_calls: int = 0
    queue: JsonObject = field(default_factory=lambda: {"queue_running": [], "queue_pending": []})

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/userdata/komaquill/session.json":
            return httpx.Response(200, json=self.session.model_dump(mode="json"))
        if path == "/history":
            return httpx.Response(200, json=self.history)
        if path == "/queue":
            return httpx.Response(200, json=self.queue)
        if path == "/upload/image":
            self.upload_calls += 1
            name, folder, content = multipart_input(
                request.headers["content-type"], request.content
            )
            self.uploaded_inputs["/".join(part for part in (folder, name) if part)] = content
            return httpx.Response(200, json={"name": name, "subfolder": folder, "type": "input"})
        if path == "/prompt":
            self.submissions += 1
            data = json.loads(request.content)
            prompt_id = str(uuid4())
            self.history[prompt_id] = {
                "prompt": [0, prompt_id, data["prompt"], data["extra_data"], ["9"]],
                "outputs": {
                    "9": {
                        "images": [
                            {
                                "filename": "../../outside.png"
                                if index == 0
                                else f"candidate-{index}.png",
                                "subfolder": "",
                                "type": "output",
                            }
                            for index in range(batch_count(data["prompt"]))
                        ]
                    }
                },
                "status": {"status_str": "success", "completed": True},
            }
            if self.drop_response:
                raise httpx.ReadTimeout("connection interrupted", request=request)
            return httpx.Response(200, json={"prompt_id": prompt_id})
        if path == "/view":
            if request.url.params.get("type") == "input":
                key = "/".join(
                    part
                    for part in (request.url.params["subfolder"], request.url.params["filename"])
                    if part
                )
                return httpx.Response(200, content=self.uploaded_inputs[key])
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
        if path == "/object_info":
            return httpx.Response(200, json={})
        if path == "/system_stats":
            return httpx.Response(200, json={"devices": [{"type": "cuda"}]})
        return httpx.Response(404)
