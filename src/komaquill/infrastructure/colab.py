"""Control owned runtimes with the official Colab CLI and OpenSSH."""

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psutil
from filelock import FileLock, ReadWriteLock
from pydantic import BaseModel

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import AllocationRefused, FailureInfo, KomaquillError, failure_info
from komaquill.domain.models import RuntimeObservation
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.deployment import Payload, create_payload
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.provenance import source_revision
from komaquill.infrastructure.storage import atomic_json

COLAB_CLI_VERSION = "0.7.5"
PROCESS_EXIT_GRACE = 3.0


class Receipt(BaseModel):
    """Retain ownership until shutdown is confirmed without storing tokens."""

    name: str
    runtime_id: UUID
    source_revision: str | None = None
    code_sha256: str | None = None
    payload_sha256: str | None = None
    endpoint_id: str | None = None
    account_sha256: str | None = None
    settings: RuntimeSettings | None = None


class Colab:
    """Operate only the runtime allocated by this workspace."""

    def __init__(self, project: Project) -> None:
        self.project = project
        identity = hashlib.sha256(str(project.root).encode()).hexdigest()[:16]
        self.config = Path.home() / ".config/colab-cli/komaquill" / identity / "sessions.json"
        self.receipt_file = project.state / "colab.json"
        self.lock = FileLock(str(project.state / ".colab.lock"))
        saved = self.receipt()
        self.settings = (
            saved.settings if saved and saved.settings is not None else project.settings()
        )

    def command(self, *arguments: str) -> list[str]:
        """Invoke the pinned official CLI in the current Python environment."""
        try:
            installed = version("google-colab-cli")
        except PackageNotFoundError:
            raise KomaquillError(
                display_key="text.colab_cli_is_unavailable_install_the_colab_client"
            ) from None
        if installed != COLAB_CLI_VERSION:
            raise KomaquillError(
                display_key="text.the_colab_cli_version_does_not_match_synchronize"
            )
        # The official CLI initializes its lock before creating the parent directory.
        self.config.parent.mkdir(parents=True, exist_ok=True)
        return [
            sys.executable,
            "-m",
            "komaquill.infrastructure.colab_entry",
            "--config",
            str(self.config),
            *arguments,
        ]

    def call(
        self, *arguments: str, interactive: bool = False, timeout: float | None = None
    ) -> None:
        """Keep official CLI response bodies out of logs and exceptions."""
        try:
            process = subprocess.Popen(
                self.command(*arguments),
                stdin=None if interactive else subprocess.DEVNULL,
                stdout=None if interactive else subprocess.PIPE,
                stderr=None if interactive else subprocess.PIPE,
            )
            timeout = timeout or (
                self.settings.preparation_timeout
                if arguments[0] == "new"
                else self.settings.control_timeout
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except BaseException:
                terminate(process, self.settings.control_timeout)
                raise
        except subprocess.TimeoutExpired:
            raise KomaquillError(
                display_key="text.the_colab_operation_timed_out_check_status_and"
            ) from None
        if process.returncode:
            output = (stdout or b"") + (stderr or b"")
            if b"Allocation refused" in output:
                raise AllocationRefused(
                    display_key="text.colab_refused_allocation_check_quota_availability_and_active"
                )
            if b"Backend rejected accelerator" in output:
                raise AllocationRefused(
                    display_key="text.the_requested_gpu_is_unavailable_check_the_colab"
                )
            raise KomaquillError(
                display_key="text.the_colab_operation_failed_check_authentication_and_runtime"
            )

    def receipt(self) -> Receipt | None:
        """Read the ownership receipt."""
        if not self.receipt_file.is_file():
            return None
        return Receipt.model_validate_json(self.receipt_file.read_bytes())

    def entry(self, receipt: Receipt) -> dict[str, Any] | None:
        """Compare the owned identity with official session state."""
        if not self.config.is_file():
            return None
        with ReadWriteLock(str(self.config) + ".lock", is_singleton=False).read_lock():
            data: dict[str, Any] = json.loads(self.config.read_text(encoding="utf-8"))
        entry: dict[str, Any] | None = data.get(receipt.name)
        if entry is not None and receipt.endpoint_id not in {None, entry.get("endpoint")}:
            raise KomaquillError(
                display_key="text.the_runtime_identity_differs_from_ownership_records_the"
            )
        return entry

    def provider_command(self, action: str, *arguments: str) -> list[str]:
        """Use the pinned adapter with public identities, never tokens, in arguments."""
        self.command("version")
        if action == "identity":
            return [
                sys.executable,
                "-m",
                "komaquill.infrastructure.colab_bridge",
                action,
                "--request-timeout",
                str(self.settings.request_timeout),
            ]
        return [
            sys.executable,
            "-m",
            "komaquill.infrastructure.colab_bridge",
            action,
            "--receipt",
            str(self.receipt_file),
            *arguments,
        ]

    def provider_call(
        self, action: str, *arguments: str, timeout: float | None = None
    ) -> RuntimeObservation:
        """Bound provider calls even if an upstream helper has no request timeout."""
        try:
            result = subprocess.run(
                self.provider_command(action, *arguments),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=timeout or self.settings.control_timeout,
            )
            if result.returncode:
                failure = FailureInfo.model_validate(json.loads(result.stderr)["failure"])
                raise KomaquillError(
                    code=failure.code,
                    stage=failure.stage,
                    http_status=failure.http_status,
                    retryable=failure.retryable,
                )
            return RuntimeObservation.model_validate_json(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise KomaquillError(code="colab_failed", stage="colab_control") from None

    def refresh(self, receipt: Receipt) -> bool:
        """Observe provider allocation; missing CLI cache is never shutdown evidence."""
        observation = self.provider_call("observe")
        if (
            observation.state == "unknown"
            or not receipt.endpoint_id
            or not receipt.account_sha256
            or observation.endpoint_id != receipt.endpoint_id
            or observation.account_sha256 != receipt.account_sha256
        ):
            raise KomaquillError(code="identity_mismatch", stage="colab_control")
        return observation.state == "running"

    def acquire(self, revision: str) -> tuple[Receipt, bool]:
        """Allocate only from explicit startup, or reconnect to the owned runtime."""
        self.project.state.mkdir(parents=True, exist_ok=True)
        with self.lock:
            receipt = self.receipt()
            if receipt is not None:
                if not self.refresh(receipt):
                    raise KomaquillError(
                        display_key="text.the_previous_runtime_has_ended_run_stop_then"
                    )
                if receipt.code_sha256 != revision:
                    raise KomaquillError(
                        display_key="text.the_gpu_payload_differs_run_stop_then_start"
                    )
                return receipt, False
            receipt = Receipt(
                name=f"komaquill-{uuid4().hex}",
                runtime_id=uuid4(),
                source_revision=source_revision(),
                code_sha256=revision,
                settings=self.settings,
            )
            # Authenticate and initialize official locking before allocation.
            authenticated = (Path.home() / ".config/colab-cli/token.json").is_file()
            self.call("status", "-s", receipt.name, interactive=not authenticated)
            identity = self.provider_call("identity")
            if not identity.account_sha256:
                raise KomaquillError(code="authentication_failed")
            receipt.account_sha256 = identity.account_sha256
            atomic_json(self.receipt_file, receipt.model_dump(mode="json"))
            # Delegate initial authentication and credentials to the official CLI.
            authenticated = (Path.home() / ".config/colab-cli/token.json").is_file()
            try:
                self.call(
                    "new",
                    "-s",
                    receipt.name,
                    "--gpu",
                    self.settings.gpu,
                    interactive=not authenticated,
                )
            except AllocationRefused:
                self.receipt_file.unlink()
                raise
            entry = self.entry(receipt)
            if entry is None:
                raise KomaquillError(
                    display_key="text.colab_allocation_is_unconfirmed_check_status_and_run"
                )
            receipt.endpoint_id = str(entry["endpoint"])
            atomic_json(self.receipt_file, receipt.model_dump(mode="json"))
            return receipt, True

    def stop(self) -> RuntimeObservation:
        """Terminate and confirm the owned endpoint under the recorded account."""
        self.project.state.mkdir(parents=True, exist_ok=True)
        with self.lock:
            receipt = self.receipt()
            if receipt is None:
                raise KomaquillError(code="identity_mismatch")
            if receipt.endpoint_id is None:
                entry = self.entry(receipt)
                if entry is not None:
                    receipt.endpoint_id = str(entry["endpoint"])
                    atomic_json(self.receipt_file, receipt.model_dump(mode="json"))
            observation = self.provider_call("terminate")
            if (
                not observation.stopped
                or observation.endpoint_id != receipt.endpoint_id
                or observation.account_sha256 != receipt.account_sha256
            ):
                raise KomaquillError(code="identity_mismatch", stage="gpu_stop")
            return observation

    def release(self, receipt: Receipt) -> None:
        """Release ownership after persistence and cleanup have completed."""
        with self.lock:
            current = self.receipt()
            if current is not None and current.runtime_id != receipt.runtime_id:
                raise KomaquillError(
                    display_key="text.the_shutdown_target_changed_ownership_records_were_retained"
                )
            self.receipt_file.unlink(missing_ok=True)

    def cleanup_processes(self, receipt: Receipt) -> None:
        """Clean up owned CLI processes after an interrupted monitor."""
        for process in psutil.process_iter(["cmdline"]):
            try:
                arguments = process.info["cmdline"] or []
                if "-m" not in arguments:
                    continue
                module = arguments[arguments.index("-m") + 1 :][:1]
                if module == ["komaquill.infrastructure.colab_bridge"]:
                    if "--receipt" in arguments and arguments[arguments.index("--receipt") + 1 :][
                        :1
                    ] == [str(self.receipt_file)]:
                        terminate_tree(process, self.settings.control_timeout)
                    continue
                if module != ["komaquill.infrastructure.colab_entry"]:
                    continue
                if "--config" not in arguments or "-s" not in arguments:
                    continue
                config = arguments[arguments.index("--config") + 1 :][:1]
                name = arguments[arguments.index("-s") + 1 :][:1]
                if config != [str(self.config)] or name != [receipt.name]:
                    continue
                terminate_tree(process, self.settings.control_timeout)
            except psutil.NoSuchProcess:
                continue

    def prepare(self, receipt: Receipt, payload: Payload | None = None) -> subprocess.Popen[bytes]:
        """Transfer a verified package snapshot without depending on a Git checkout."""
        payload = payload or create_payload(self.project.configured(self.settings))
        with self.lock:
            if self.entry(receipt) is None:
                raise KomaquillError(display_key="text.the_runtime_is_unavailable_check_status")
            receipt.payload_sha256 = payload.sha256
            atomic_json(self.receipt_file, receipt.model_dump(mode="json"))
            self.call(
                "upload", "-s", receipt.name, str(payload.path), "content/komaquill-runtime.tar"
            )
        remote_root = f"/content/komaquill-{receipt.runtime_id}"
        script = self.project.runtime / "deployment/prepare.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "import hashlib, json, os, subprocess, sys, tarfile\n"
            "with open('/content/komaquill-runtime.tar', 'rb') as archive:\n"
            f"    if hashlib.file_digest(archive, 'sha256').hexdigest() != {payload.sha256!r}:\n"
            "        raise RuntimeError('Transferred payload failed SHA256 verification')\n"
            f"root = {remote_root!r}\n"
            "os.makedirs(root, exist_ok=True)\n"
            "with tarfile.open('/content/komaquill-runtime.tar') as bundle:\n"
            "    bundle.extractall(root, filter='data')\n"
            "with open(root + '/manifest.json') as handle:\n"
            "    manifest = json.load(handle)\n"
            "os.environ['KOMAQUILL_SOURCE_REVISION'] = manifest.get('source_revision') or ''\n"
            "os.environ['KOMAQUILL_CODE_SHA256'] = manifest['code_sha256']\n"
            f"os.environ['KOMAQUILL_RUNTIME_ID'] = {str(receipt.runtime_id)!r}\n"
            "os.makedirs(root + '/.artifacts/logs', exist_ok=True)\n"
            "with open(root + '/.artifacts/logs/bootstrap.log', 'ab') as log:\n"
            "    subprocess.run([sys.executable, '-m', 'komaquill.gpu.bootstrap'],\n"
            "                   cwd=root, stdout=log, stderr=subprocess.STDOUT, check=True)\n",
            encoding="utf-8",
        )
        with self.lock:
            if self.entry(receipt) is None:
                raise KomaquillError(display_key="text.the_runtime_is_unavailable_check_status")
            return subprocess.Popen(
                self.command(
                    "exec",
                    "-s",
                    receipt.name,
                    "-f",
                    str(script),
                    "--timeout",
                    str(self.settings.worker_execution_timeout),
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def progress(self, receipt: Receipt) -> dict[str, str | None]:
        """Read bounded structured progress from an existing runtime only."""
        target = self.project.artifacts / "tmp" / f"progress-{receipt.runtime_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.provider_call(
                "download",
                f"content/komaquill-{receipt.runtime_id}/.artifacts/logs/progress.json",
                str(target),
                timeout=self.settings.request_timeout,
            )
            data = json.loads(target.read_bytes())
            allowed = {
                "environment",
                "dependencies",
                "comfyui",
                "model_preparation",
                "server",
                "ready",
            }
            phase = data.get("phase")
            asset = data.get("asset")
            if phase not in allowed or (asset is not None and not isinstance(asset, str)):
                raise ValueError
            return {"phase": phase, "asset": asset}
        except (KomaquillError, OSError, ValueError):
            return {}
        finally:
            try:
                target.unlink(missing_ok=True)
            except OSError as error:
                record_event(
                    self.project,
                    "progress_cleanup",
                    "failed",
                    runtime_id=receipt.runtime_id,
                    failure=failure_info(error, "preparation"),
                )

    def recover_diagnostics(self, receipt: Receipt) -> tuple[str, ...]:
        """Download independent snapshots and preserve the last successful capture."""
        folder = self.project.artifacts / "logs" / str(receipt.runtime_id)
        folder.mkdir(parents=True, exist_ok=True)
        remote = f"content/komaquill-{receipt.runtime_id}/.artifacts"
        failed: list[str] = []
        capture = folder / "capture.json"
        try:
            previous = json.loads(capture.read_bytes()) if capture.is_file() else {}
        except (OSError, ValueError):
            previous = {}
        raw_files = previous.get("files", {}) if isinstance(previous, dict) else {}
        files = raw_files if isinstance(raw_files, dict) else {}
        started = datetime.now(UTC).isoformat()
        deadline = time.monotonic() + self.settings.control_timeout
        for source, name in [
            ("logs/bootstrap.log", "bootstrap.log"),
            ("logs/comfy.log", "comfy.log"),
            ("logs/cli.jsonl", "worker-events.jsonl"),
            ("runtime/environment.txt", "environment.txt"),
        ]:
            temporary = folder / f".{name}.{uuid4().hex}.tmp"
            prior = files.get(name)
            prior = prior if isinstance(prior, dict) else {}
            item = {
                "attempted_at": started,
                "result": "failed",
                "last_success_at": prior.get("last_success_at"),
            }
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise KomaquillError(
                        display_key="text.log_collection_timed_out", code="log_timeout"
                    )
                with self.lock:
                    self.provider_call(
                        "download",
                        f"{remote}/{source}",
                        str(temporary),
                        timeout=remaining,
                    )
                temporary.replace(folder / name)
                item.update(result="saved", last_success_at=datetime.now(UTC).isoformat())
            except (KomaquillError, OSError) as error:
                failed.append(name)
                if isinstance(error, KomaquillError):
                    if error.code == "request_not_found":
                        item["result"] = (
                            "missing" if prior.get("last_success_at") else "not_created"
                        )
                    elif error.code == "runtime_ended":
                        item["result"] = "unavailable"
                item["failure"] = failure_info(error, "log_collection").model_dump(mode="json")
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as error:
                    if name not in failed:
                        failed.append(name)
                    item.update(
                        result="failed",
                        failure=failure_info(error, "log_cleanup").model_dump(mode="json"),
                    )
            if (folder / name).is_file():
                item["size_bytes"] = (folder / name).stat().st_size
            files[name] = item
        atomic_json(
            capture,
            {
                "captured_at": datetime.now(UTC).isoformat(),
                "runtime_id": str(receipt.runtime_id),
                "files": files,
                "failed": failed,
            },
        )
        record_event(
            self.project,
            "log_collection",
            "failed" if failed else "saved",
            runtime_id=receipt.runtime_id,
        )
        return tuple(failed)

    def tunnel(self, receipt: Receipt) -> subprocess.Popen[bytes]:
        """Share one SSH connection bound to the loopback interface."""
        ssh = shutil.which("ssh")
        keygen = shutil.which("ssh-keygen")
        if not ssh or not keygen:
            raise KomaquillError(display_key="text.install_the_openssh_client")
        folder = self.project.state / "ssh"
        folder.mkdir(parents=True, exist_ok=True)
        key = folder / "id_ed25519"
        if not key.is_file():
            subprocess.run(
                [keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                check=True,
                capture_output=True,
            )
        with self.lock:
            # Provider evidence replaces optional local CLI cache for reconnection.
            if receipt.endpoint_id is None or receipt.account_sha256 is None:
                raise KomaquillError(
                    display_key="text.the_runtime_is_unavailable_ssh_was_not_started"
                )
            proxy_arguments = self.provider_command("proxy", str(key))
            proxy = (
                subprocess.list2cmdline(proxy_arguments)
                if os.name == "nt"
                else shlex.join(proxy_arguments)
            )
            settings = self.settings
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
            # Suppress provider stderr and expose only safe product errors.
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
                            display_key="text.ssh_failed_check_authentication_and_runtime_ssh_support"
                        )
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", settings.local_port), timeout=1
                        ):
                            # A listening forward confirms SSH authentication and session selection.
                            return process
                    except OSError:
                        time.sleep(settings.poll_interval)
                raise KomaquillError(display_key="text.ssh_connection_timed_out_check_status")
            except BaseException:
                terminate(process, settings.control_timeout)
                raise


def check_port(port: int) -> None:
    """Detect port conflicts before allocation."""
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    except OSError:
        raise KomaquillError(display_key="text.the_local_port_is_in_use_check_the") from None


def terminate(process: subprocess.Popen[bytes], timeout: float) -> None:
    """Terminate owned children, including the Windows Python launcher."""
    if process.poll() is not None:
        return
    try:
        parent = psutil.Process(process.pid)
        if process.poll() is not None:
            return
        terminate_tree(parent, timeout)
    except psutil.NoSuchProcess:
        pass
    process.wait(timeout=timeout)


def terminate_tree(parent: psutil.Process, timeout: float) -> None:
    """Terminate verified process trees within the deadline."""
    children = parent.children(recursive=True)
    for child in reversed(children):
        with suppress(psutil.NoSuchProcess):
            child.terminate()
    parent.terminate()
    _, alive = psutil.wait_procs([parent, *children], timeout=min(timeout, PROCESS_EXIT_GRACE))
    for child in alive:
        with suppress(psutil.NoSuchProcess):
            child.kill()
    _, alive = psutil.wait_procs(alive, timeout=timeout)
    if alive:
        raise KomaquillError(display_key="text.cannot_stop_the_local_connection_process_run_stop")
