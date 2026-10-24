"""Preserve file revisions locally and restore only saved workflows and their inputs."""

import contextlib
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import keyring
from filelock import FileLock

from komaquill.domain.errors import KomaquillError, failure_info
from komaquill.domain.materials import Material
from komaquill.infrastructure.comfyui import ComfyClient
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.diagnostics import record_event
from komaquill.infrastructure.storage import atomic_bytes, atomic_json, load_session

PREFIX = "/komaquill/v1"


def records(project: Project, kind: str) -> list[Material]:
    """Return immutable revisions without changing previously saved metadata."""
    return sorted(
        (
            Material.model_validate_json(p.read_bytes())
            for p in (project.data / kind / "records").glob("*.json")
        ),
        key=lambda r: r.observed_at,
    )


def blob(project: Project, row: Material) -> Path:
    return (
        project.data
        / row.kind
        / row.sha256
        / ("workflow.json" if row.kind == "workflows" else "content")
    )


class MaterialCollector:
    """Keep local acknowledgement behind durable content and metadata writes."""

    def __init__(
        self, project: Project, client: ComfyClient, headers: dict[str, str] | None = None
    ) -> None:
        if headers is None:
            token = keyring.get_password("komaquill-extension", str(project.root))
            if token is None:
                raise KomaquillError(code="authentication_failed", stage="storage")
            headers = {"X-Komaquill-Control": token}
        self.project, self.client, self.headers = project, client, headers
        self.states: dict[str, str] = {}

    def collect(self) -> list[str]:
        data = self.client.json(PREFIX + "/files")
        failures = list(data.get("errors", []))
        for index, entry in enumerate(data["files"]):
            try:
                row = Material.model_validate(entry)
            except ValueError:
                failures.append(f"material-index:{index}")
                continue
            path = self.project.data / row.kind / "records" / f"{row.material_id}.json"
            if row.state == "saved" and path.is_file() and blob(self.project, row).is_file():
                continue
            try:
                operational = self.project.state / "preservation" / f"{row.material_id}.json"
                if row.runtime_id is None and self.project.session_file.is_file():
                    row.runtime_id = load_session(self.project).runtime_id
                atomic_json(operational, row.model_dump(mode="json"))
                with FileLock(str(self.project.state / ".materials.lock")):
                    target = blob(self.project, row)
                    if not path.is_file() or not target.is_file():
                        content = self.client.request(
                            "GET", PREFIX + f"/files/{row.material_id}"
                        ).content
                        if (
                            hashlib.sha256(content).hexdigest() != row.sha256
                            or len(content) != row.size_bytes
                        ):
                            raise KomaquillError(code="invalid_output", stage="storage")
                        atomic_bytes(target, content)
                        row.state = "saved"
                        atomic_json(path, row.model_dump(mode="json"))
                    self.client.request(
                        "POST",
                        PREFIX + f"/files/{row.material_id}",
                        headers=self.headers,
                        json={"sha256": row.sha256},
                    )
                    row.state = "saved"
                    atomic_json(operational, row.model_dump(mode="json"))
            except (OSError, KomaquillError) as error:
                failures.append(str(row.material_id))
                row.failure = failure_info(error, "storage")
                with contextlib.suppress(OSError):
                    atomic_json(operational, row.model_dump(mode="json"))
                record_event(
                    self.project,
                    "preservation",
                    "failed",
                    operation_id=row.material_id,
                    failure=failure_info(error, "storage"),
                )
        return failures

    def notify_runs(self) -> None:
        """Notify only observed state changes, never optimistic local-save success."""
        from komaquill.infrastructure.storage import load_runs, load_session

        if not self.project.session_file.is_file():
            return
        session = load_session(self.project)
        preserved_inputs = {row.sha256 for row in records(self.project, "inputs")}
        for run in load_runs(self.project):
            if run.runtime_id != session.runtime_id or run.prompt_id is None:
                continue
            missing_inputs = bool(set(run.input_sha256.values()) - preserved_inputs)
            state = (
                "retry"
                if run.failure or missing_inputs
                else "saved"
                if run.state == "saved"
                else "saving"
            )
            if self.states.get(run.prompt_id) != state:
                self.client.request(
                    "POST",
                    PREFIX + "/save-status",
                    headers=self.headers,
                    json={"state": state, "prompt_id": run.prompt_id},
                )
                self.states[run.prompt_id] = state

    def restore(self) -> None:
        """Restore latest named workflows with content-addressed input references."""
        latest = {row.name: row for row in records(self.project, "workflows")}
        inputs = {(row.name, row.sha256): row for row in records(self.project, "inputs")}
        for row in latest.values():
            graph = json.loads(blob(self.project, row).read_bytes())
            for node in graph.get("nodes", []):
                if node.get("type") not in {"LoadImage", "LoadImageMask"}:
                    continue
                values = node.get("widgets_values", [])
                if not values or not isinstance(values[0], str):
                    continue
                relative = values[0].removesuffix(" [input]")
                source = inputs.get((relative, row.input_sha256.get(relative, "")))
                if source is None:
                    raise KomaquillError(code="request_not_found", stage="storage")
                name = Path(source.name).name
                with blob(self.project, source).open("rb") as handle:
                    response = self.client.request(
                        "POST",
                        "/upload/image",
                        headers=self.headers,
                        data={
                            "type": "input",
                            "subfolder": f"komaquill/{source.sha256}",
                            "overwrite": "true",
                        },
                        files={"image": (name, handle)},
                    ).json()
                values[0] = (response.get("subfolder", "") + "/" + response["name"]).lstrip("/")
            remote = "workflows/komaquill/" + row.sha256 + ".json"
            self.client.request(
                "POST",
                "/userdata/" + quote(remote, safe=""),
                headers=self.headers,
                params={"overwrite": "true"},
                json=graph,
            )


def finalize_materials(project: Project, runtime_id: object) -> tuple[str, ...]:
    """Mark only an ended owned runtime's unpreserved revisions as lost."""
    lost = []
    with FileLock(str(project.state / ".materials.lock")):
        for path in (project.state / "preservation").glob("*.json"):
            row = Material.model_validate_json(path.read_bytes())
            if str(row.runtime_id) == str(runtime_id) and row.state != "saved":
                row.state = "lost"
                row.failure = failure_info(
                    KomaquillError(code="runtime_ended", retryable=False), "storage"
                )
                atomic_json(path, row.model_dump(mode="json"))
                lost.append(str(row.material_id))
    return tuple(lost)
