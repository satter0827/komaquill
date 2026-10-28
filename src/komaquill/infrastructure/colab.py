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
from typing import Any, TypeVar
from uuid import UUID, uuid4

import psutil
from filelock import FileLock, ReadWriteLock
from pydantic import AwareDatetime, BaseModel

from komaquill.domain.configuration import RecordedSettings, RuntimeSettings, remaining_seconds
from komaquill.domain.errors import AllocationRefused, FailureInfo, KomaquillError, failure_info
from komaquill.domain.models import RuntimeObservation
from komaquill.domain.usage import AccountUsage
from komaquill.infrastructure.atomic import atomic_json
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.deployment import Payload, create_payload
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.model_auth import credentials
from komaquill.infrastructure.monitor_state import check_cancel
from komaquill.infrastructure.paths import GpuPaths
from komaquill.infrastructure.provenance import source_revision

COLAB_CLI_VERSION = "0.7.5"
PROCESS_EXIT_GRACE = 3.0
ProviderResult = TypeVar("ProviderResult", bound=BaseModel)


class Receipt(BaseModel):
    """Retain ownership until shutdown is confirmed without storing tokens."""

    name: str
    runtime_id: UUID
    source_revision: str | None = None
    code_sha256: str | None = None
    payload_sha256: str | None = None
    endpoint_id: str | None = None
    account_sha256: str | None = None
    settings: RecordedSettings | None = None
    started_at: AwareDatetime | None = None


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
            timeout = self.bounded_timeout(
                timeout
                or (
                    self.settings.preparation_timeout
                    if arguments[0] == "new"
                    else self.settings.control_timeout
                )
            )
            process = subprocess.Popen(
                self.command(*arguments),
                stdin=None if interactive else subprocess.DEVNULL,
                stdout=None if interactive else subprocess.PIPE,
                stderr=None if interactive else subprocess.PIPE,
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

    def bounded_timeout(self, requested: float) -> float:
        """Use the persisted session clock, including startup and reconnection."""
        receipt = self.receipt()
        remaining = remaining_seconds(
            receipt.started_at if receipt else None,
            self.settings.session_timeout,
            datetime.now(UTC),
        )
        return requested if remaining is None else min(requested, max(1.0, remaining))

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
        if action in {"identity", "usage"}:
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

    def _provider_call(
        self,
        result_type: type[ProviderResult],
        action: str,
        *arguments: str,
        timeout: float | None = None,
    ) -> ProviderResult:
        """Bound provider calls even if an upstream helper has no request timeout."""
        try:
            result = subprocess.run(
                self.provider_command(action, *arguments),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=self.bounded_timeout(timeout or self.settings.control_timeout),
            )
            if result.returncode:
                failure = FailureInfo.model_validate(json.loads(result.stderr)["failure"])
                raise KomaquillError(
                    code=failure.code,
                    stage=failure.stage,
                    http_status=failure.http_status,
                    retryable=failure.retryable,
                )
            return result_type.model_validate_json(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise KomaquillError(code="colab_failed", stage="colab_control") from None

    def provider_call(
        self, action: str, *arguments: str, timeout: float | None = None
    ) -> RuntimeObservation:
        """Read an ownership observation through the bounded provider adapter."""
        return self._provider_call(RuntimeObservation, action, *arguments, timeout=timeout)

    def usage(self) -> AccountUsage:
        """Read account consumption without allocating or connecting to a runtime."""
        return self._provider_call(AccountUsage, "usage", timeout=self.settings.request_timeout)

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

    def acquire(
        self, revision: str, *, expected_account: str | None = None
    ) -> tuple[Receipt, bool]:
        """Allocate only from explicit startup, or reconnect to the owned runtime."""
        self.project.state.mkdir(parents=True, exist_ok=True)
        with self.lock:
            receipt = self.receipt()
            if receipt is not None:
                if expected_account is not None and receipt.account_sha256 != expected_account:
                    raise KomaquillError(code="identity_mismatch", stage="colab_control")
                if not self.refresh(receipt):
                    raise KomaquillError(
                        display_key="text.the_previous_runtime_has_ended_run_stop_then"
                    )
                if receipt.code_sha256 != revision:
                    raise KomaquillError(
                        display_key="text.the_gpu_payload_differs_run_stop_then_start"
                    )
                return receipt, False
            if not isinstance(self.settings, RuntimeSettings):
                raise KomaquillError(code="invalid_configuration", stage="colab_control")
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
            if expected_account is not None and identity.account_sha256 != expected_account:
                raise KomaquillError(code="identity_mismatch", stage="colab_control")
            receipt.account_sha256 = identity.account_sha256
            receipt.started_at = datetime.now(UTC)
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
                    *(["--high-mem"] if self.settings.high_ram else []),
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
            current = self.receipt()
            if current is None or current.runtime_id != receipt.runtime_id:
                raise KomaquillError(code="identity_mismatch", stage="preparation")
            if self.entry(receipt) is None:
                raise KomaquillError(display_key="text.the_runtime_is_unavailable_check_status")
            receipt.payload_sha256 = payload.sha256
            atomic_json(self.receipt_file, receipt.model_dump(mode="json"))
        atomic_json(
            self.project.state / f"startup-{receipt.runtime_id}.json", {"phase": "payload_ready"}
        )
        check_cancel(self.project)
        self.call("upload", "-s", receipt.name, str(payload.path), "content/komaquill-runtime.tar")
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
            "from pathlib import Path\n"
            "os.environ['KOMAQUILL_SOURCE_REVISION'] = manifest.get('source_revision') or ''\n"
            "os.environ['KOMAQUILL_CODE_SHA256'] = manifest['code_sha256']\n"
            f"os.environ['KOMAQUILL_RUNTIME_ID'] = {str(receipt.runtime_id)!r}\n"
            "os.environ['KOMAQUILL_SESSION_STARTED_AT'] = "
            f"{receipt.started_at.isoformat() if receipt.started_at else ''!r}\n"
            "os.environ['KOMAQUILL_OPERATION_ID'] = "
            f"{os.environ.get('KOMAQUILL_OPERATION_ID', '')!r}\n"
            "sys.path.insert(0, root)\n"
            "from komaquill.infrastructure.paths import GpuPaths\n"
            "from pathlib import Path\n"
            "logs = GpuPaths(Path(root)).logs\n"
            "logs.mkdir(parents=True, exist_ok=True)\n",
            encoding="utf-8",
        )
        with self.lock:
            check_cancel(self.project)
            current = self.receipt()
            if current is None or current.runtime_id != receipt.runtime_id:
                raise KomaquillError(code="identity_mismatch", stage="preparation")
            if self.entry(receipt) is None:
                raise KomaquillError(display_key="text.the_runtime_is_unavailable_check_status")
        self.call(
            "exec",
            "-s",
            receipt.name,
            "-f",
            str(script),
            "--timeout",
            str(self.settings.control_timeout),
        )
        atomic_json(
            self.project.state / f"startup-{receipt.runtime_id}.json",
            {"phase": "bootstrap_requested"},
        )
        return self.remote_models(receipt, None)

    def remote_models(
        self, receipt: Receipt, job_id: UUID | None, *, timeout: float | None = None
    ) -> subprocess.Popen[bytes]:
        """Send credentials only to verified owned code through the SSH input stream."""
        ssh = shutil.which("ssh")
        if ssh is None:
            raise KomaquillError(display_key="text.install_the_openssh_client")
        with self.lock:
            current = self.receipt()
            if (
                current is None
                or current.runtime_id != receipt.runtime_id
                or self.entry(receipt) is None
            ):
                raise KomaquillError(code="identity_mismatch", stage="preparation", retryable=False)
        root = f"/content/komaquill-{receipt.runtime_id}"
        module = "komaquill.gpu.bootstrap" if job_id is None else "komaquill.gpu.models"
        python = "python3" if job_id is None else root + "/.artifacts/runtime/venv/bin/python"
        log_name = "bootstrap.log" if job_id is None else f"models-{job_id}.log"
        from komaquill.infrastructure.monitor_state import read_monitor

        session_operation_id = os.environ.get("KOMAQUILL_OPERATION_ID", "")
        with suppress(OSError, ValueError):
            monitor = read_monitor(self.project)
            if monitor and monitor.runtime_id == receipt.runtime_id:
                session_operation_id = str(monitor.operation_id)
        script = (
            "import json,os,sys; "
            f"root={root!r}; sys.path.insert(0,root); "
            "manifest=json.load(open(root+'/manifest.json')); "
            f"assert manifest['code_sha256']=={receipt.code_sha256!r}; "
            f"os.environ['KOMAQUILL_RUNTIME_ID']={str(receipt.runtime_id)!r}; "
            f"os.environ['KOMAQUILL_SESSION_OPERATION_ID']={session_operation_id!r}; "
            "os.environ['KOMAQUILL_OPERATION_ID']="
            f"{str(job_id) if job_id else os.environ.get('KOMAQUILL_OPERATION_ID', '')!r}; "
            "os.environ['KOMAQUILL_CODE_SHA256']=manifest['code_sha256']; "
            "os.environ['KOMAQUILL_SOURCE_REVISION']=manifest.get('source_revision') or ''; "
            "os.environ['KOMAQUILL_SESSION_STARTED_AT']="
            f"{receipt.started_at.isoformat() if receipt.started_at else ''!r}; "
            f"sys.argv=[{module!r},root,{str(job_id) if job_id else ''!r}]; "
            "import subprocess,pathlib; "
            "tokens=sys.stdin.buffer.readline(65536); "
            "env=dict(os.environ); env['PYTHONPATH']=root; "
            f"log=open({root + '/.artifacts/logs/' + log_name!r},'ab'); "
            f"child=subprocess.Popen([{python!r},'-u','-m',{module!r}]"
            + (f"+[{root!r},{str(job_id)!r}]" if job_id else "")
            + ",cwd=root,env=env,stdin=subprocess.PIPE,"
            "stdout=log,stderr=subprocess.STDOUT,start_new_session=True); "
            "child.stdin.write(tokens); child.stdin.close()"
        )
        command = f"cd {shlex.quote(root)} && {shlex.quote(python)} -c {shlex.quote(script)}"
        process = subprocess.Popen(
            [
                ssh,
                "-T",
                *self.ssh_options(receipt, self.ssh_identity()),
                "root@colab-runtime",
                command,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(credentials()).encode() + b"\n")
            process.stdin.close()
            process.wait(
                timeout=self.bounded_timeout(
                    min(self.settings.control_timeout, timeout)
                    if timeout is not None
                    else self.settings.control_timeout
                )
            )
            if process.returncode:
                raise KomaquillError(code="connection_failed", stage="preparation", retryable=False)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            terminate(process, self.settings.control_timeout)
            raise KomaquillError(code="connection_failed", stage="preparation") from None
        return process

    def progress(self, receipt: Receipt) -> dict[str, str | None]:
        """Read bounded structured progress from an existing runtime only."""
        target = self.project.artifacts / "tmp" / f"progress-{receipt.runtime_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.provider_call(
                "download",
                (
                    GpuPaths(Path(f"content/komaquill-{receipt.runtime_id}")).logs / "progress.json"
                ).as_posix(),
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
                "failed",
            }
            phase = data.get("phase")
            asset = data.get("asset")
            if phase not in allowed or (asset is not None and not isinstance(asset, str)):
                raise ValueError
            state = {"phase": data.get("previous_phase") if phase == "failed" else phase}
            if phase == "failed":
                state["failure"] = FailureInfo.model_validate(data["failure"]).model_dump(
                    mode="json"
                )
            if phase == "failed" and data.get("last_failure") is not None:
                with suppress(ValueError, KeyError, TypeError):
                    state["last_failure"] = FailureInfo.model_validate(
                        data["last_failure"]
                    ).model_dump(mode="json")
                    state["last_failure_at"] = datetime.fromisoformat(
                        data["last_failure_at"]
                    ).isoformat()
            atomic_json(self.project.state / f"startup-{receipt.runtime_id}.json", state)
            result = {"phase": phase, "asset": asset}
            if phase == "failed":
                failure = FailureInfo.model_validate(data["failure"])
                result.update(
                    failure_code=failure.code.value,
                    failure_stage=failure.stage.value,
                    http_status=str(failure.http_status)
                    if failure.http_status is not None
                    else None,
                )
            return result
        except (KomaquillError, OSError, ValueError, KeyError):
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
        folder = self.project.logs / str(receipt.runtime_id)
        folder.mkdir(parents=True, exist_ok=True)
        remote = GpuPaths(Path(f"content/komaquill-{receipt.runtime_id}"))
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
            (remote.logs / "bootstrap.log", "bootstrap.log"),
            (remote.logs / "comfy.log", "comfy.log"),
            (remote.logs / "cli.jsonl", "worker-events.jsonl"),
            (remote.runtime / "environment.txt", "environment.txt"),
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
                        source.as_posix(),
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
                        phase_file = self.project.state / f"startup-{receipt.runtime_id}.json"
                        try:
                            phase = json.loads(phase_file.read_bytes()).get("phase")
                        except (OSError, ValueError, AttributeError):
                            phase = None
                        producers = {
                            "bootstrap.log": {
                                "environment",
                                "dependencies",
                                "comfyui",
                                "model_preparation",
                                "server",
                                "ready",
                            },
                            "comfy.log": {"server", "ready"},
                            "worker-events.jsonl": {"model_preparation", "server", "ready"},
                            "environment.txt": {"ready"},
                        }
                        known = {
                            "payload_ready",
                            "bootstrap_requested",
                            "environment",
                            "dependencies",
                            "comfyui",
                            "model_preparation",
                            "server",
                            "ready",
                        }
                        if (
                            not prior.get("last_success_at")
                            and phase in known
                            and phase not in producers[name]
                        ):
                            failed.remove(name)
                    elif error.code == "runtime_ended":
                        item["result"] = "unavailable"
                item["failure"] = failure_info(error, "log_collection").model_dump(mode="json")
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as error:
                    if name not in failed:
                        failed.append(name)
                    item["cleanup_failure"] = failure_info(error, "log_cleanup").model_dump(
                        mode="json"
                    )
                    item["result"] = "failed"
            if (folder / name).is_file():
                item["size_bytes"] = (folder / name).stat().st_size
            files[name] = item
        snapshot = {
            "captured_at": datetime.now(UTC).isoformat(),
            "runtime_id": str(receipt.runtime_id),
            "files": files,
            "failed": failed,
        }
        with FileLock(str(folder / ".capture.lock")):
            atomic_json(capture, snapshot)
            with (folder / "captures.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"capture_id": uuid4().hex, **snapshot}) + "\n")
                handle.flush()
        record_event(
            self.project,
            "log_collection",
            "failed" if failed else "saved",
            runtime_id=receipt.runtime_id,
        )
        return tuple(failed)

    def ssh_identity(self) -> Path:
        """Prepare one workspace-owned key for the pinned official SSH bridge."""
        keygen = shutil.which("ssh-keygen")
        if keygen is None:
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
        return key

    def ssh_options(self, receipt: Receipt, key: Path) -> list[str]:
        """Bind standard OpenSSH clients to the recorded endpoint and identity."""
        if receipt.endpoint_id is None or receipt.account_sha256 is None:
            raise KomaquillError(display_key="text.the_runtime_is_unavailable_ssh_was_not_started")
        proxy_arguments = self.provider_command("proxy", str(key))
        proxy = (
            subprocess.list2cmdline(proxy_arguments)
            if os.name == "nt"
            else shlex.join(proxy_arguments)
        )
        return [
            "-i",
            str(key),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={key.parent / 'known_hosts'}",
            "-o",
            f"HostKeyAlias={receipt.endpoint_id}",
            "-o",
            f"ProxyCommand={proxy}",
        ]

    def tunnel(self, receipt: Receipt) -> subprocess.Popen[bytes]:
        """Share one SSH connection bound to the loopback interface."""
        ssh = shutil.which("ssh")
        if not ssh:
            raise KomaquillError(display_key="text.install_the_openssh_client")
        key = self.ssh_identity()
        with self.lock:
            options = self.ssh_options(receipt, key)
            settings = self.settings
            arguments = [
                ssh,
                "-N",
                "-T",
                *options,
                "-o",
                "ExitOnForwardFailure=yes",
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
            deadline = time.monotonic() + self.bounded_timeout(settings.control_timeout)
            try:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise KomaquillError(
                            display_key="text.ssh_failed_check_authentication_and_runtime_ssh_support",
                            code="connection_failed",
                            stage="connection",
                            retryable=False,
                        )
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", settings.local_port), timeout=1
                        ):
                            # A listening forward confirms SSH authentication and session selection.
                            return process
                    except OSError:
                        time.sleep(min(settings.poll_interval, max(0, deadline - time.monotonic())))
                raise KomaquillError(
                    display_key="text.ssh_connection_timed_out_check_status",
                    code="connection_failed",
                    stage="connection",
                    retryable=False,
                )
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
