"""Clean only identified recreatable storage while retaining data and evidence."""

import json
import os
import shutil
import subprocess
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from uuid import uuid4

import psutil
from filelock import FileLock, Timeout

from komaquill.application.cleanup import exclusion
from komaquill.domain.cleanup import CleanupItem
from komaquill.domain.errors import KomaquillError, failure_info
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.monitor_state import alive, read_monitor
from komaquill.infrastructure.provenance import code_sha256, source_revision
from komaquill.infrastructure.storage import atomic_json


def measured(path: Path) -> tuple[int, int, bool]:
    """Measure regular files without descending into links or junctions."""
    count = size = 0
    linked = path.is_symlink() or path.is_junction()
    if linked:
        return 0, 0, True
    if path.is_file():
        return 1, path.stat().st_size, False
    for folder, directories, files in os.walk(path, followlinks=False):
        for name in list(directories):
            child = Path(folder) / name
            if child.is_symlink() or child.is_junction():
                linked = True
                directories.remove(name)
        for name in files:
            child = Path(folder) / name
            if child.is_symlink():
                linked = True
                continue
            size += child.stat().st_size
            count += 1
    return count, size, linked


def cache_in_use(project: Project) -> bool:
    """Detect lexical venv paths and Python workspace use without logging process arguments."""

    def within(value: str, root: Path) -> bool:
        path = Path(value)
        return path.is_absolute() and path.absolute().is_relative_to(root.absolute())

    for process in psutil.process_iter(["pid", "name"]):
        if process.pid == os.getpid():
            continue
        python = (process.info.get("name") or "").lower().startswith(("python", "pypy"))
        try:
            executable = process.exe()
            arguments = process.cmdline()
            if within(executable, project.runtime) or any(
                within(argument, project.runtime) for argument in arguments
            ):
                return True
            python = python or Path(executable).name.lower().startswith(("python", "pypy"))
            if not python:
                continue
            if within(process.cwd(), project.root) or any(
                within(argument, project.root) for argument in arguments
            ):
                return True
            environment = process.environ()
            runtime_hints = [
                environment.get("VIRTUAL_ENV", ""),
                *environment.get("PATH", "").split(os.pathsep),
            ]
            workspace_hints = [
                environment.get("KOMAQUILL_PROJECT_DIR", ""),
                *environment.get("PYTHONPATH", "").split(os.pathsep),
            ]
            if any(within(hint, project.runtime) for hint in runtime_hints) or any(
                within(hint, project.root) for hint in workspace_hints
            ):
                return True
            if any(
                within(file.path, folder)
                for file in process.open_files()
                for folder in (project.cache, project.temporary)
            ):
                return True
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            if python:
                return True
    return False


def assert_stopped(project: Project) -> None:
    """Do not allocate, reconnect or stop a runtime during cleanup."""
    migration = project.state / "storage-migration.json"
    if migration.is_file() and json.loads(migration.read_bytes()).get("status") != "complete":
        raise KomaquillError(display_key="cleanup.migration", code="invalid_configuration")
    if alive(read_monitor(project)) or (project.state / "colab.json").exists():
        raise KomaquillError(display_key="cleanup.busy", code="invalid_request")


def plan(project: Project) -> tuple[CleanupItem, ...]:
    """Protect every latest successful distribution and unknown directory."""
    assert_stopped(project)
    protected = set()
    for pointer in (project.reports / "quality").glob("latest-*.json"):
        protected.add(json.loads(pointer.read_bytes())["report"])
    busy = cache_in_use(project)
    result = []
    for category in ("cache", "tmp", "dist"):
        kind: Literal["cache", "tmp", "dist"] = category
        for path in (
            sorted((project.artifacts / category).iterdir())
            if (project.artifacts / category).is_dir()
            else []
        ):
            count, size, linked = measured(path)
            gate = project.reports / "quality" / path.name / "gate.json"
            known = False
            if gate.is_file():
                try:
                    evidence = json.loads(gate.read_bytes())
                    known = evidence.get("group") in {"quality", "all"} and isinstance(
                        evidence.get("steps"), list
                    )
                except (OSError, ValueError, AttributeError):
                    pass
            reason = exclusion(category, path.name, protected, known, busy)
            if linked:
                reason = "linked"
            if not path.is_dir():
                reason = "unknown"
            result.append(
                CleanupItem(
                    path.relative_to(project.artifacts).as_posix(), kind, size, count, reason
                )
            )
    return tuple(result)


def execute(project: Project, dry_run: bool) -> tuple[tuple[CleanupItem, ...], Path | None, bool]:
    """Persist the deletion plan before deleting any eligible directory."""
    project.state.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        try:
            for name in (".supervisor-start.lock", ".monitor.lock", ".cleanup.lock"):
                stack.enter_context(FileLock(str(project.state / name), timeout=0))
        except Timeout:
            raise KomaquillError(display_key="cleanup.busy", code="invalid_request") from None
        items = plan(project)
        if dry_run:
            return items, None, False
        report = project.reports / "cleanup" / f"{uuid4().hex}.json"
        record = {
            "format_version": 1,
            "operation_id": report.stem,
            "app_version": version("komaquill"),
            "source_revision": source_revision(),
            "code_sha256": code_sha256(),
            "started_at": datetime.now(UTC).isoformat(),
            "items": [asdict(row) for row in items],
            "results": [],
        }
        atomic_json(report, record)
        outcomes: list[dict[str, object]] = []
        updated = []
        failed = False
        for item in items:
            if item.reason is not None:
                updated.append(item)
                continue
            path = project.artifacts / item.relative_path
            try:
                root = project.artifacts.resolve()
                if not path.resolve().is_relative_to(root) or measured(path)[2]:
                    raise KomaquillError(code="invalid_request")
                assert_stopped(project)
                if item.category == "cache" and path.name == "uv":
                    bundled = project.runtime / "uv" / ("uv.exe" if os.name == "nt" else "uv")
                    uv = os.environ.get("KOMAQUILL_UV") or (
                        str(bundled) if bundled.is_file() else shutil.which("uv")
                    )
                    if not uv:
                        raise KomaquillError(code="invalid_configuration")
                    subprocess.run(
                        [uv, "cache", "prune", "--cache-dir", str(path)],
                        check=True,
                        capture_output=True,
                    )
                else:
                    shutil.rmtree(path)
                outcomes.append({"path": item.relative_path, "result": "completed"})
                updated.append(replace(item, result="completed"))
            except (OSError, KomaquillError, subprocess.SubprocessError) as error:
                failed = True
                updated.append(replace(item, result="failed"))
                outcomes.append(
                    {
                        "path": item.relative_path,
                        "result": "failed",
                        "exception_type": type(error).__name__,
                        "failure": failure_info(error, "cleanup").model_dump(mode="json"),
                    }
                )
        record.update(
            results=outcomes,
            finished_at=datetime.now(UTC).isoformat(),
            status="failed" if failed else "completed",
        )
        atomic_json(report, record)
        return tuple(updated), report, failed
