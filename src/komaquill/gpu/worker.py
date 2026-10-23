"""Run the pinned ComfyUI environment on the GPU runtime."""

import importlib
import os
import subprocess
import sys
import time
import tomllib
from importlib.resources import files
from pathlib import Path

import httpx

from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import JsonObject
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.downloads import fetch_model
from komaquill.infrastructure.storage import atomic_json
from komaquill.infrastructure.workflows import preset, resource_json
from komaquill.messages import error_message, message


def prepare_server(project: Project, config: JsonObject, user: Path) -> Path:
    """Pin the server source and register standard workflow templates."""
    runtime = project.artifacts / "runtime"
    comfy = runtime / "ComfyUI"
    if not comfy.exists():
        subprocess.run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                "https://github.com/Comfy-Org/ComfyUI.git",
                str(comfy),
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(comfy), "checkout", "--detach", config["comfy_revision"]], check=True
        )
    actual = subprocess.check_output(
        ["git", "-C", str(comfy), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != config["comfy_revision"]:
        raise KomaquillError(display_key="text.the_comfyui_revision_differs_start_a_new_runtime")
    extension = comfy / "custom_nodes/komaquill"
    templates = extension / "example_workflows"
    templates.mkdir(parents=True, exist_ok=True)
    (extension / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n")
    for view in ["graph", "app"]:
        workflow = resource_json("workflow.json")
        workflow["extra"]["linearMode"] = view == "app"
        atomic_json(templates / f"sd15_{view}.json", workflow)
    model = fetch_model(project.artifacts, preset())
    paths = {"komaquill": {"base_path": str(model.parent), "checkpoints": "."}}
    # JSON is a YAML subset, so the model path file needs no conversion.
    atomic_json(runtime / "model-paths.json", paths)
    user.mkdir(parents=True, exist_ok=True)
    return comfy


def main(root: Path) -> None:
    """Wait for ComfyUI and keep caches inside the temporary runtime."""
    torch = importlib.import_module("torch")
    if not torch.cuda.is_available():
        raise KomaquillError(display_key="text.no_gpu_was_found_check_runtime_gpu_availability")
    project = Project(root.resolve())
    config = tomllib.loads(files("komaquill").joinpath("resources/runtime.toml").read_text())
    runtime = project.runtime
    runtime.mkdir(parents=True, exist_ok=True)
    user = runtime / "comfy-user"
    comfy = prepare_server(project, config, user)
    metadata = {
        "runtime_id": os.environ["KOMAQUILL_RUNTIME_ID"],
        "source_revision": os.environ.get("KOMAQUILL_SOURCE_REVISION") or None,
        "code_sha256": os.environ.get("KOMAQUILL_CODE_SHA256"),
        "comfy_revision": config["comfy_revision"],
        "frontend_version": config["frontend_version"],
        "model_sha256": preset().sha256,
    }
    atomic_json(user / "default/komaquill/session.json", metadata)
    logs = project.artifacts / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    port = project.settings().remote_port
    with (logs / "comfy.log").open("ab") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "main.py",
                "--listen",
                "127.0.0.1",
                "--port",
                str(port),
                "--user-directory",
                str(user),
                "--output-directory",
                str(project.artifacts / "output"),
                "--input-directory",
                str(project.artifacts / "input"),
                "--temp-directory",
                str(project.artifacts / "tmp"),
                "--extra-model-paths-config",
                str(runtime / "model-paths.json"),
                "--disable-auto-launch",
            ],
            cwd=comfy,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + project.settings().preparation_timeout
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise KomaquillError(
                    display_key="text.comfyui_exited_check_artifacts_logs_comfy_log"
                )
            try:
                if httpx.get(f"http://127.0.0.1:{port}/system_stats", timeout=2).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(project.settings().poll_interval)
        else:
            raise KomaquillError(display_key="text.comfyui_startup_timed_out")
        with (runtime / "environment.txt").open("w") as handle:
            subprocess.run(
                [str(runtime / "tools/uv"), "pip", "freeze", "--python", sys.executable],
                stdout=handle,
                check=True,
            )
        print(message("text.comfyui_started"), flush=True)
        result = server.wait()
        if result:
            raise KomaquillError(display_key="text.comfyui_exited_check_its_execution_log")
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=project.settings().control_timeout)


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]))
    except KomaquillError as error:
        print(error_message(error), file=sys.stderr)
        raise SystemExit(1) from None
