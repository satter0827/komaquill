"""Run the pinned ComfyUI environment on the GPU runtime."""

import importlib
import json
import os
import subprocess
import sys
import time
import tomllib
from importlib.resources import files
from pathlib import Path

import httpx

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import JsonObject
from komaquill.domain.presets import MODEL_ROLES
from komaquill.gpu.progress import progress
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.downloads import fetch_model
from komaquill.infrastructure.storage import atomic_json
from komaquill.infrastructure.workflows import assets, prepare
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
    (extension / "__init__.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "from server import PromptServer\n"
        "from komaquill.gpu.extension import register\n"
        "from importlib.resources import files\n"
        "NODE_CLASS_MAPPINGS = {}\n"
        "WEB_DIRECTORY = str(files('komaquill').joinpath('resources/web'))\n"
        "register(PromptServer.instance, Path(os.environ['KOMAQUILL_ARTIFACTS']), "
        "float(os.environ['KOMAQUILL_REQUEST_TIMEOUT']))\n"
    )
    os.environ["KOMAQUILL_ARTIFACTS"] = str(project.artifacts)
    os.environ["KOMAQUILL_REQUEST_TIMEOUT"] = str(project.settings().request_timeout)
    settings = project.settings()
    for name in settings.profiles:
        profile = settings.presets[name]
        for view in ["graph", "app"]:
            _, workflow = prepare(
                profile.sample_prompt, None, 1, name, settings=settings, profile_name=name
            )
            workflow["extra"]["linearMode"] = view == "app"
            atomic_json(templates / f"{name}_{view}.json", workflow)
    paths = {
        "managed": {
            "base_path": str(project.artifacts / "models"),
            **{role: role for role in MODEL_ROLES},
        }
    }
    for asset in assets(settings):
        progress(project.root, "model_preparation", asset.name)
        model = fetch_model(project.artifacts, asset)
        paths[asset.name] = {"base_path": str(model.parent.parent), asset.role: "."}
    # JSON is a YAML subset, so the model path file needs no conversion.
    atomic_json(runtime / "model-paths.json", paths)
    user.mkdir(parents=True, exist_ok=True)
    return comfy


def main(root: Path) -> None:
    """Wait for ComfyUI and keep caches inside the temporary runtime."""
    torch = importlib.import_module("torch")
    if not torch.cuda.is_available():
        raise KomaquillError(display_key="text.no_gpu_was_found_check_runtime_gpu_availability")
    manifest = root / "manifest.json"
    effective = (
        RuntimeSettings.model_validate(json.loads(manifest.read_bytes())["settings"])
        if manifest.is_file()
        else None
    )
    project = Project(root.resolve(), effective)
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
        "model_sha256": project.settings()
        .assets[project.settings().presets[project.settings().profiles[0]].checkpoint]
        .sha256,
    }
    atomic_json(user / "default/komaquill/session.json", metadata)
    logs = project.artifacts / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    progress(root, "server")
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
        progress(root, "ready")
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
