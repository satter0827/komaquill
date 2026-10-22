"""GPU側で固定したComfyUIを起動する。"""

import importlib
import os
import subprocess
import sys
import time
import tomllib
from importlib.resources import files
from pathlib import Path

import httpx

from komaquill.downloads import fetch_model
from komaquill.errors import KomaquillError
from komaquill.models import JsonObject
from komaquill.settings import Project
from komaquill.storage import atomic_json
from komaquill.workflow import preset, resource_json


def prepare_server(project: Project, config: JsonObject, user: Path) -> Path:
    """ソースを固定し、標準テンプレートを登録する。"""
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
        raise KomaquillError(
            "ComfyUIのリビジョンが一致しません。新しいランタイムで起動してください。"
        )
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
    # JSONはYAMLの部分集合なので追加の変換層を作らない。
    atomic_json(runtime / "model-paths.json", paths)
    user.mkdir(parents=True, exist_ok=True)
    return comfy


def main(root: Path) -> None:
    """実行中のComfyUIを待ち、ランタイム内だけにキャッシュする。"""
    torch = importlib.import_module("torch")
    if not torch.cuda.is_available():
        raise KomaquillError("GPUが見つかりません。利用可能なGPUを確認してください。")
    project = Project(root.resolve())
    config = tomllib.loads(files("komaquill").joinpath("resources/runtime.toml").read_text())
    runtime = project.runtime
    runtime.mkdir(parents=True, exist_ok=True)
    user = runtime / "comfy-user"
    comfy = prepare_server(project, config, user)
    metadata = {
        "runtime_id": os.environ["KOMAQUILL_RUNTIME_ID"],
        "source_revision": os.environ["KOMAQUILL_SOURCE_REVISION"],
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
                    "ComfyUIが終了しました。artifacts/logs/comfy.logを確認してください。"
                )
            try:
                if httpx.get(f"http://127.0.0.1:{port}/system_stats", timeout=2).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(project.settings().poll_interval)
        else:
            raise KomaquillError("ComfyUIの起動時間を超えました。")
        with (runtime / "environment.txt").open("w") as handle:
            subprocess.run(
                [str(runtime / "tools/uv"), "pip", "freeze", "--python", sys.executable],
                stdout=handle,
                check=True,
            )
        print("ComfyUIを起動しました。", flush=True)
        result = server.wait()
        if result:
            raise KomaquillError("ComfyUIが終了しました。実行ログを確認してください。")
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=project.settings().control_timeout)


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]))
    except KomaquillError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
