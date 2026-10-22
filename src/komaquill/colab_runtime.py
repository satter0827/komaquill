"""Colab上でComfyUIと認証付き接続を準備する。"""

import base64
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import tomllib
from contextlib import closing
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import httpx

from komaquill.client import ComfyClient
from komaquill.downloads import fetch_model
from komaquill.errors import KomaquillError
from komaquill.models import JsonObject
from komaquill.service import source_revision
from komaquill.settings import Project
from komaquill.storage import atomic_json
from komaquill.workflow import preset, resource_json


def download_tool(folder: Path, name: str, spec: dict[str, str]) -> Path:
    """リリース資産をSHA256で検証して導入する。"""
    target = folder / name
    if target.is_file():
        return target
    folder.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        response = client.get(spec["url"])
        response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != spec["sha256"]:
        raise KomaquillError("配布物のSHA256が一致しません。")
    if "member" in spec:
        archive = folder / f"{name}.tar.gz"
        archive.write_bytes(response.content)
        with tarfile.open(archive) as bundle:
            member = bundle.extractfile(spec["member"])
            if member is None:
                raise KomaquillError("配布物に必要なプログラムがありません。")
            target.write_bytes(member.read())
    else:
        target.write_bytes(response.content)
    target.chmod(0o700)
    return target


def launch(arguments: list[str], cwd: Path, log_path: Path) -> subprocess.Popen[bytes]:
    """秘密情報を子プロセスの環境へ引き継がず起動する。"""
    child_env = {key: value for key, value in os.environ.items() if key != "KOMAQUILL_PASSWORD"}
    with log_path.open("ab") as log:
        return subprocess.Popen(
            arguments,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=child_env,
            start_new_session=True,
        )


def wait_http(url: str, process: subprocess.Popen[bytes], timeout: float) -> None:
    """起動直後のサービスが応答するまで待つ。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise KomaquillError(
                "サービスが終了しました。artifacts/runtimeのログを確認してください。"
            )
        try:
            if httpx.get(url, timeout=2).status_code in {200, 401}:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise KomaquillError("起動時間を超えました。artifacts/runtimeのログを確認してください。")


def verify_auth(proxy: str, password: str) -> None:
    """HTTPとWebSocketの未認証・誤認証・正しい認証を検査する。"""
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    for auth in [None, ("komaquill", "incorrect")]:
        if httpx.get(proxy + "/system_stats", auth=auth, timeout=10).status_code != 401:
            raise KomaquillError("未認証の接続を拒否できないため公開を停止しました。")
    with closing(ComfyClient(proxy, password)) as client:
        client.json("/system_stats")
    socket_url = proxy.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    for candidate in [None, "incorrect", password]:
        headers = {}
        if candidate is not None:
            encoded = base64.b64encode(f"komaquill:{candidate}".encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
        try:
            with connect(socket_url, additional_headers=headers, open_timeout=10) as socket:
                if candidate != password:
                    raise KomaquillError("WebSocketの認証を確認できないため公開を停止しました。")
                socket.recv(timeout=10)
        except InvalidStatus as exc:
            if candidate == password or exc.response.status_code != 401:
                raise KomaquillError("WebSocketの接続確認に失敗しました。") from exc


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
    """GPU確認後、認証を検証してからトンネルを開く。"""
    torch = importlib.import_module("torch")

    project = Project(root.resolve())
    config = tomllib.loads(files("komaquill").joinpath("resources/runtime.toml").read_text())
    password = os.environ.pop("KOMAQUILL_PASSWORD", "")
    if len(password) < 20:
        raise KomaquillError("Colab Secretsに20文字以上のKOMAQUILL_PASSWORDを設定してください。")
    if not torch.cuda.is_available():
        raise KomaquillError("GPUがありません。ColabのランタイムでGPUを選択してください。")
    runtime = project.artifacts / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    proxy = f"http://127.0.0.1:{config['proxy_port']}"
    prior = runtime / "remote.json"
    if prior.is_file():
        metadata = json.loads(prior.read_text())
        if metadata["source_revision"] != source_revision(project):
            raise KomaquillError("ソースが変わりました。新しいランタイムで起動してください。")
        with closing(ComfyClient(metadata["endpoint"], password)) as client:
            if client.runtime()["runtime_id"] == metadata["runtime_id"]:
                print(f"接続先URL: {metadata['endpoint']}")
                return
    user = runtime / "comfy-user"
    comfy = prepare_server(project, config, user)
    metadata = {
        "runtime_id": str(uuid4()),
        "source_revision": source_revision(project),
        "comfy_revision": config["comfy_revision"],
        "frontend_version": config["frontend_version"],
        "model_sha256": preset().sha256,
    }
    atomic_json(user / "default/komaquill/session.json", metadata)
    tools = runtime / "tools"
    caddy = download_tool(tools, "caddy", config["caddy"])
    cloudflared = download_tool(tools, "cloudflared", config["cloudflared"])
    hashed = (
        subprocess.run(
            [str(caddy), "hash-password"], input=password.encode(), capture_output=True, check=True
        )
        .stdout.decode()
        .strip()
    )
    caddyfile = runtime / "Caddyfile"
    caddyfile.write_text(
        "{\n admin off\n}\n"
        + proxy
        + " {\n basic_auth {\n  komaquill "
        + hashed
        + "\n }\n reverse_proxy 127.0.0.1:"
        + str(config["comfy_port"])
        + "\n}\n"
    )
    output = project.artifacts / "output"
    processes: list[subprocess.Popen[bytes]] = []
    try:
        server = launch(
            [
                sys.executable,
                "main.py",
                "--listen",
                "127.0.0.1",
                "--port",
                str(config["comfy_port"]),
                "--user-directory",
                str(user),
                "--output-directory",
                str(output),
                "--input-directory",
                str(project.artifacts / "input"),
                "--temp-directory",
                str(project.artifacts / "tmp"),
                "--extra-model-paths-config",
                str(runtime / "model-paths.json"),
                "--disable-auto-launch",
            ],
            comfy,
            runtime / "comfy.log",
        )
        processes.append(server)
        wait_http(
            f"http://127.0.0.1:{config['comfy_port']}/system_stats",
            server,
            config["startup_timeout"],
        )
        gateway = launch(
            [str(caddy), "run", "--config", str(caddyfile), "--adapter", "caddyfile"],
            runtime,
            runtime / "caddy.log",
        )
        processes.append(gateway)
        wait_http(proxy + "/system_stats", gateway, config["startup_timeout"])
        verify_auth(proxy, password)
        tunnel_log = runtime / "tunnel.log"
        tunnel_log.write_text("")
        tunnel = launch(
            [str(cloudflared), "tunnel", "--no-autoupdate", "--url", proxy], runtime, tunnel_log
        )
        processes.append(tunnel)
        deadline = time.monotonic() + config["startup_timeout"]
        endpoint = None
        while time.monotonic() < deadline and tunnel.poll() is None:
            match = re.search(
                r"https://[a-z0-9-]+\.trycloudflare\.com", tunnel_log.read_text(errors="replace")
            )
            if match:
                endpoint = match.group()
                break
            time.sleep(1)
        if endpoint is None:
            raise KomaquillError("接続先URLを取得できません。トンネルのログを確認してください。")
        # 公開経路でも認証を確認する。認証が確認できるまでURLは表示しない。
        public_ready = False
        while time.monotonic() < deadline:
            try:
                verify_auth(endpoint, password)
                public_ready = True
                break
            except (KomaquillError, httpx.HTTPError, OSError):
                time.sleep(2)
        if not public_ready:
            raise KomaquillError("公開経路の認証を確認できないため停止しました。")
        metadata["endpoint"] = endpoint
        atomic_json(prior, metadata)
        with (runtime / "environment.txt").open("w") as handle:
            subprocess.run(
                [str(tools / "uv"), "pip", "freeze", "--python", sys.executable],
                stdout=handle,
                check=True,
            )
        print(f"接続先URL: {endpoint}")
        print(
            "ローカルでstartにURLとパスワードを入力してください。利用後はランタイムを終了してください。"
        )
    except BaseException:
        for process in reversed(processes):
            process.terminate()
        raise


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]))
    except KomaquillError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
