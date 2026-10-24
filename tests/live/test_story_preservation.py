"""Compare retained originals against an explicitly selected local baseline."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.mark.skipif(
    not os.environ.get("KOMAQUILL_PRESERVATION_BASELINE"),
    reason="Requires a retained local preservation inventory",
)
def test_retained_originals():
    root = Path(__file__).resolve().parents[2]
    baseline_file = Path(os.environ["KOMAQUILL_PRESERVATION_BASELINE"])
    baseline = json.loads(baseline_file.read_bytes())
    result = {
        "observed_at": datetime.now(UTC).isoformat(),
        "unchanged": [],
        "appended": [],
        "derived": [],
        "changed": [],
        "missing": [],
    }
    for name, original in baseline["files"].items():
        path = root / name
        if not path.is_file():
            result["missing"].append(name)
            continue
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest == original["sha256"]:
            result["unchanged"].append(name)
        elif name.startswith(".artifacts/records/logs/"):
            with path.open("rb") as handle:
                prefix = hashlib.sha256(handle.read(original["size_bytes"])).hexdigest()
            result["appended" if prefix == original["sha256"] else "changed"].append(name)
        elif name == ".artifacts/data/locations.json" or (
            name.startswith(".artifacts/data/stories/")
            and path.name in {"README.md", "story.md", "manifest.json"}
        ):
            result["derived"].append(name)
        else:
            result["changed"].append(name)
    settings = (root / ".vscode/settings.json").read_text(encoding="utf-8")
    result["personal_settings_preserved"] = settings == baseline["personal_settings"]
    from uuid import UUID

    from komaquill.infrastructure.locations import Locations
    from komaquill.infrastructure.paths import WorkspacePaths

    locations = Locations(WorkspacePaths(root))
    placement_errors = []
    with locations.snapshot():
        for name in baseline["files"]:
            path = root / name
            if not path.is_file():
                continue
            if path.name == "run.json":
                identity = UUID(json.loads(path.read_bytes())["run_id"])
                if locations.run(identity) != path.parent:
                    placement_errors.append(name)
            elif path.name == "request.json" and name.startswith(".artifacts/data/stories/"):
                identity = UUID(json.loads(path.read_bytes())["story_id"])
                if locations.story(identity) != path.parent:
                    placement_errors.append(name)
    result["placement_errors"] = placement_errors
    result["baseline_sha256"] = hashlib.sha256(baseline_file.read_bytes()).hexdigest()
    target = (
        root
        / ".artifacts/records/reports/story-evidence"
        / f"{baseline_file.stem}-verification.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    assert not result["changed"] and not result["missing"], result
    assert result["personal_settings_preserved"]
    assert not placement_errors
