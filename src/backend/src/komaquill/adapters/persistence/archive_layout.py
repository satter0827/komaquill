"""Finish verified archive relocation without overwriting originals or guessing missing files."""

import json
from pathlib import Path

from komaquill.adapters.atomic import atomic_json
from komaquill.adapters.digests import file_digest
from komaquill.adapters.paths import WorkspacePaths, artifact_path
from komaquill.domain.errors import KomaquillError

ARCHIVE_CATEGORIES = frozenset(
    {
        "runs",
        "stories",
        "inputs",
        "workflows",
        "image-preparations",
        "conversation-calls",
        "conversation-usage",
        "evaluations",
        "locations.json",
    }
)


def pending(project: WorkspacePaths) -> Path:
    return project.state / "archive-migration.json"


def begin(project: WorkspacePaths, report: Path, files: dict[str, str]) -> None:
    atomic_json(
        pending(project),
        {"format_version": 1, "report": str(report.resolve()), "status": "pending", "files": files},
    )


def finish(project: WorkspacePaths) -> Path:
    value = json.loads(pending(project).read_bytes())
    report = Path(value["report"]).resolve()
    if not report.is_relative_to((project.records / "migrations").resolve()):
        raise KomaquillError(code="invalid_request", stage="storage")
    after, relocations = {}, {}
    for key, expected in value["files"].items():
        target_key = "archive/" + key if key.split("/")[0] in ARCHIVE_CATEGORIES else key
        source = artifact_path(project.data, key)
        target = artifact_path(project.data, target_key)
        if target_key != key:
            if target.exists():
                if file_digest(target) != expected:
                    raise KomaquillError(code="identity_mismatch", stage="storage")
                if source.exists():
                    if file_digest(source) != expected:
                        raise KomaquillError(code="identity_mismatch", stage="storage")
                    source.unlink()
            else:
                if file_digest(source) != expected:
                    raise KomaquillError(code="identity_mismatch", stage="storage")
                target.parent.mkdir(parents=True, exist_ok=True)
                source.rename(target)
        if file_digest(target) != expected:
            raise KomaquillError(code="identity_mismatch", stage="storage")
        after[target_key], relocations[key] = expected, target_key
    record = json.loads(report.read_bytes())
    atomic_json(
        report, {**record, "files_after": after, "relocations": relocations, "state": "complete"}
    )
    atomic_json(pending(project), {**value, "status": "complete"})
    return report
