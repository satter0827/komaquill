"""公式Colab CLIとOpenSSHによるランタイムの制御。"""

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from filelock import FileLock, ReadWriteLock
from pydantic import BaseModel

from komaquill.errors import KomaquillError
from komaquill.settings import Project
from komaquill.storage import atomic_json

COLAB_CLI_VERSION = "0.7.5"


class Receipt(BaseModel):
    """終了確認まで保持する所有記録。トークンは保存しない。"""

    name: str
    runtime_id: UUID
    source_revision: str
    endpoint_id: str | None = None


class Colab:
    """プロジェクトが確保したランタイムだけを操作する。"""

    def __init__(self, project: Project) -> None:
        self.project = project
        identity = hashlib.sha256(str(project.root).encode()).hexdigest()[:16]
        self.config = Path.home() / ".config/colab-cli/komaquill" / identity / "sessions.json"
        self.receipt_file = project.runtime / "colab.json"
        self.lock = FileLock(str(project.runtime / ".colab.lock"))

    def command(self, *arguments: str) -> list[str]:
        """固定した公式CLIを同じPython環境で呼ぶ。"""
        try:
            installed = version("google-colab-cli")
        except PackageNotFoundError:
            raise KomaquillError("Colab CLIがありません。環境を準備し直してください。") from None
        if installed != COLAB_CLI_VERSION:
            raise KomaquillError("Colab CLIの版が異なります。環境を準備し直してください。")
        return [
            sys.executable,
            "-m",
            "komaquill.colab_entry",
            "--config",
            str(self.config),
            *arguments,
        ]

    def call(self, *arguments: str, interactive: bool = False) -> None:
        """公式CLIの応答本文をログや例外へ転載しない。"""
        try:
            result = subprocess.run(
                self.command(*arguments),
                stdin=None if interactive else subprocess.DEVNULL,
                stdout=None if interactive else subprocess.DEVNULL,
                stderr=None if interactive else subprocess.DEVNULL,
                timeout=self.project.settings().preparation_timeout
                if arguments[0] == "new"
                else self.project.settings().control_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise KomaquillError(
                "Colabの操作が時間内に完了しませんでした。status と stop で確認してください。"
            ) from None
        if result.returncode:
            raise KomaquillError(
                "Colabの操作に失敗しました。Google認証とランタイムの利用状態を確認してください。"
            )

    def receipt(self) -> Receipt | None:
        """所有記録だけを読む。"""
        if not self.receipt_file.is_file():
            return None
        return Receipt.model_validate_json(self.receipt_file.read_bytes())

    def entry(self, receipt: Receipt) -> dict[str, Any] | None:
        """公式のセッション状態から所有するIDを照合する。"""
        if not self.config.is_file():
            return None
        with ReadWriteLock(str(self.config) + ".lock", is_singleton=False).read_lock():
            data: dict[str, Any] = json.loads(self.config.read_text(encoding="utf-8"))
        entry: dict[str, Any] | None = data.get(receipt.name)
        if entry is not None and receipt.endpoint_id not in {None, entry.get("endpoint")}:
            raise KomaquillError("ランタイムIDが所有記録と異なります。操作を停止しました。")
        return entry

    def refresh(self, receipt: Receipt) -> bool:
        """確認によって新しいランタイムを作らない。呼び出し元がロックする。"""
        self.entry(receipt)
        self.call("status", "-s", receipt.name)
        return self.entry(receipt) is not None

    def acquire(self, revision: str) -> tuple[Receipt, bool]:
        """明示的なstartだけが新規確保する。残存時は同じ環境へ接続する。"""
        self.project.runtime.mkdir(parents=True, exist_ok=True)
        with self.lock:
            receipt = self.receipt()
            if receipt is not None:
                if not self.refresh(receipt):
                    raise KomaquillError(
                        "前回のランタイムは終了しています。stop 後に start してください。"
                    )
                if receipt.source_revision != revision:
                    raise KomaquillError("前回のソースと異なります。stop 後に start してください。")
                return receipt, False
            receipt = Receipt(
                name=f"komaquill-{uuid4().hex}", runtime_id=uuid4(), source_revision=revision
            )
            atomic_json(self.receipt_file, receipt.model_dump(mode="json"))
            # 初回だけ公式のGoogle認証を端末で行う。秘密情報を独自に扱わない。
            authenticated = (Path.home() / ".config/colab-cli/token.json").is_file()
            self.call(
                "new",
                "-s",
                receipt.name,
                "--gpu",
                self.project.settings().gpu,
                interactive=not authenticated,
            )
            entry = self.entry(receipt)
            if entry is None:
                raise KomaquillError(
                    "Colabの確保結果を確認できません。status と stop で確認してください。"
                )
            receipt.endpoint_id = str(entry["endpoint"])
            atomic_json(self.receipt_file, receipt.model_dump(mode="json"))
            return receipt, True

    def stop(self) -> None:
        """所有するIDの終了を公式CLIで確認してから記録を消す。"""
        self.project.runtime.mkdir(parents=True, exist_ok=True)
        with self.lock:
            receipt = self.receipt()
            if receipt is None:
                return
            if self.refresh(receipt):
                self.call("stop", "-s", receipt.name)
                if self.refresh(receipt):
                    raise KomaquillError(
                        "終了を確認できません。管理記録を残しました。stop で再確認してください。"
                    )
            self.receipt_file.unlink()

    def prepare(self, receipt: Receipt) -> subprocess.Popen[bytes]:
        """コミットのソースを一時転送し、GPU側の処理を実行し続ける。"""
        archive = self.project.runtime / "source.tar"
        subprocess.run(
            [
                "git",
                "-C",
                str(self.project.root),
                "archive",
                "--format=tar",
                "-o",
                str(archive),
                receipt.source_revision,
            ],
            check=True,
        )
        with self.lock:
            if self.entry(receipt) is None:
                raise KomaquillError("ランタイムがありません。status で確認してください。")
            self.call("upload", "-s", receipt.name, str(archive), "content/komaquill-source.tar")
        remote_root = f"/content/komaquill-{receipt.runtime_id}"
        with archive.open("rb") as handle:
            archive_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
        script = self.project.runtime / "prepare.py"
        script.write_text(
            "import hashlib, os, subprocess, sys, tarfile\n"
            "with open('/content/komaquill-source.tar', 'rb') as archive:\n"
            f"    assert hashlib.file_digest(archive, 'sha256').hexdigest() == {archive_sha256!r}\n"
            f"root = {remote_root!r}\n"
            "os.makedirs(root, exist_ok=True)\n"
            "with tarfile.open('/content/komaquill-source.tar') as bundle:\n"
            "    bundle.extractall(root, filter='data')\n"
            f"os.environ['KOMAQUILL_SOURCE_REVISION'] = {receipt.source_revision!r}\n"
            f"os.environ['KOMAQUILL_RUNTIME_ID'] = {str(receipt.runtime_id)!r}\n"
            f"os.environ['KOMAQUILL_REMOTE_PORT'] = {str(self.project.settings().remote_port)!r}\n"
            "os.makedirs(root + '/artifacts/logs', exist_ok=True)\n"
            "with open(root + '/artifacts/logs/bootstrap.log', 'ab') as log:\n"
            "    subprocess.run([sys.executable, root + '/scripts/worker.py'],\n"
            "                   stdout=log, stderr=subprocess.STDOUT, check=True)\n",
            encoding="utf-8",
        )
        # セルを実行中に保つ。独自のkeep-aliveは作らない。
        with self.lock:
            if self.entry(receipt) is None:
                raise KomaquillError("ランタイムがありません。status で確認してください。")
            return subprocess.Popen(
                self.command(
                    "exec",
                    "-s",
                    receipt.name,
                    "-f",
                    str(script),
                    "--timeout",
                    str(self.project.settings().execution_timeout),
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def recover_diagnostics(self, receipt: Receipt) -> None:
        """終了前にGPU側の環境と準備ログをローカルへ回収する。"""
        folder = self.project.artifacts / "logs" / str(receipt.runtime_id)
        folder.mkdir(parents=True, exist_ok=True)
        remote = f"content/komaquill-{receipt.runtime_id}/artifacts"
        for source, name in [
            ("logs/bootstrap.log", "bootstrap.log"),
            ("logs/comfy.log", "comfy.log"),
            ("runtime/environment.txt", "environment.txt"),
        ]:
            try:
                with self.lock:
                    if self.entry(receipt) is None:
                        return
                    self.call(
                        "download", "-s", receipt.name, f"{remote}/{source}", str(folder / name)
                    )
            except KomaquillError:
                return

    def tunnel(self, receipt: Receipt) -> subprocess.Popen[bytes]:
        """一つのSSH接続を共有し、ローカルの待ち受けをループバックに限定する。"""
        ssh = shutil.which("ssh")
        keygen = shutil.which("ssh-keygen")
        if not ssh or not keygen:
            raise KomaquillError("OpenSSHクライアントを導入してください。")
        folder = self.project.runtime / "ssh"
        folder.mkdir(parents=True, exist_ok=True)
        key = folder / "id_ed25519"
        if not key.is_file():
            subprocess.run(
                [keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                check=True,
                capture_output=True,
            )
        with self.lock:
            # sshには自動作成経路があるため、名前の存在とIDを照合してから呼ぶ。
            if self.entry(receipt) is None or receipt.endpoint_id is None:
                raise KomaquillError("ランタイムがありません。SSH接続は開始しませんでした。")
            proxy_arguments = self.command(
                "ssh", "--proxy-mode", "-s", receipt.name, "--identity", str(key)
            )
            proxy = (
                subprocess.list2cmdline(proxy_arguments)
                if os.name == "nt"
                else shlex.join(proxy_arguments)
            )
            settings = self.project.settings()
            arguments = [
                ssh,
                "-N",
                "-T",
                "-i",
                str(key),
                "-o",
                "BatchMode=yes",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f"UserKnownHostsFile={folder / 'known_hosts'}",
                "-o",
                f"HostKeyAlias={receipt.endpoint_id}",
                "-o",
                f"ProxyCommand={proxy}",
                "-L",
                f"127.0.0.1:{settings.local_port}:127.0.0.1:{settings.remote_port}",
                "root@colab-runtime",
            ]
            # stderrはトークンを含まない日本語表示に置き換える。
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + settings.control_timeout
            try:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise KomaquillError(
                            "SSH接続に失敗しました。Google認証とランタイムのSSH対応を確認してください。"
                        )
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", settings.local_port), timeout=1
                        ):
                            # ローカル転送が待ち受けた時点でSSH認証とセッション選択が完了している。
                            return process
                    except OSError:
                        time.sleep(settings.poll_interval)
                raise KomaquillError("SSH接続の待機時間を超えました。status で確認してください。")
            except BaseException:
                process.terminate()
                process.wait(timeout=settings.control_timeout)
                raise


def check_port(port: int) -> None:
    """新規確保前にポート競合を検出する。"""
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    except OSError:
        raise KomaquillError(
            "接続ポートが使用中です。起動済みの start または設定を確認してください。"
        ) from None
