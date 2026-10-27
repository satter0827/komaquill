"""Check evaluation provenance, immutable history and AI evidence export."""

import io
import json
from uuid import uuid4

import pytest
from PIL import Image
from pydantic import ValidationError
from typer.testing import CliRunner

from komaquill.application.evaluations import register
from komaquill.application.generation import new_run
from komaquill.domain.errors import KomaquillError
from komaquill.domain.evaluations import Evaluation, Evaluator
from komaquill.domain.models import ImageRef
from komaquill.infrastructure.evaluations import (
    FileEvaluationRepository,
    export_evidence,
    read_import,
    select,
)
from komaquill.interfaces.cli import app


@pytest.fixture
def history(project, session):
    runs = []
    for prompt in ("a lake", "a mountain"):
        graph = {"1": {"inputs": {"text": prompt}}}
        run = new_run(project, session, uuid4(), graph)
        project.repository.prepare(run, graph, {"nodes": []})
        project.repository.conditions(
            run, [0, str(uuid4()), graph, {"extra_pnginfo": {"workflow": {"nodes": []}}}]
        )
        key = f"runs/{run.run_id}/images/0001.png"
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2)).save(buffer, "PNG")
        content = buffer.getvalue()
        project.artifacts_store.write(key, content)
        run.images = ["images/0001.png"]
        run.image_metadata["images/0001.png"] = project.artifacts_store.inspect_image(
            content,
            ImageRef(filename="result.png"),
        )
        run.state = "saved"
        project.repository.save(run)
        runs.append(run)
    return runs


def evaluation(run, **kwargs):
    return Evaluation(
        run_id=run.run_id,
        criterion="prompt_adherence",
        reason="Matches the scene.",
        decision="accept",
        **kwargs,
    )


def test_provenance_and_mode_validation(history):
    assert Evaluator(
        kind="ai", model="model-v1", instruction="Assess composition."
    ).instruction_sha256
    for values in [
        {"kind": "ai"},
        {"instruction": "text", "instruction_sha256": "0" * 64},
    ]:
        with pytest.raises(ValidationError):
            Evaluator(**values)
    for values in [
        {"run_id": history[0].run_id, "criterion": "x", "reason": "x"},
        {
            "run_id": history[0].run_id,
            "comparison_run_id": history[0].run_id,
            "criterion": "x",
            "reason": "x",
            "preference": "first",
        },
        {
            "run_id": history[0].run_id,
            "criterion": "x",
            "reason": "x",
            "decision": "accept",
            "preference": "first",
        },
    ]:
        with pytest.raises(ValidationError):
            Evaluation(**values)
    identity = uuid4()
    with pytest.raises(ValidationError):
        evaluation(history[0], evaluation_id=identity, supersedes=identity)


def test_registration_is_append_only_and_reimport_is_idempotent(project, history):
    repository = FileEvaluationRepository(project)
    first = evaluation(history[0])
    register(project.repository, repository, [first])
    path = project.evaluations / f"{first.evaluation_id}.json"
    original = path.read_bytes()
    register(project.repository, repository, [first])
    assert path.read_bytes() == original
    correction = evaluation(history[0], supersedes=first.evaluation_id)
    register(project.repository, repository, [correction])
    assert len(select(project, history[0].run_id)) == 2
    assert select(project, history[1].run_id) == []
    with pytest.raises(KomaquillError):
        register(project.repository, repository, [first.model_copy(update={"reason": "changed"})])
    with pytest.raises(KomaquillError):
        repository.append(first.model_copy(update={"reason": "changed"}))
    assert path.read_bytes() == original


@pytest.mark.parametrize("case", ["missing", "pending", "image", "fingerprint", "supersedes"])
def test_invalid_references_do_not_write_evaluations(project, history, case):
    run = history[0]
    item = evaluation(run)
    if case == "missing":
        item.run_id = uuid4()
    elif case == "pending":
        run.state = "queued"
        project.repository.save(run)
    elif case == "image":
        item.image = "../outside.png"
    elif case == "fingerprint":
        item.workflow_sha256 = {str(run.run_id): "wrong"}
    else:
        item.supersedes = uuid4()
    with pytest.raises(KomaquillError):
        register(project.repository, FileEvaluationRepository(project), [item])
    assert not list(project.evaluations.glob("*.json"))


def test_export_includes_unevaluated_runs_and_real_conditions(project, history, tmp_path):
    data = export_evidence(project)
    assert len(data["runs"]) == 2 and data["evaluations"] == []
    assert data["evaluation_schema"] and data["artifact_base"]["relative_to"] == "workspace"
    assert {row["workflow_api"]["1"]["inputs"]["text"] for row in data["runs"]} == {
        "a lake",
        "a mountain",
    }
    assert all(row["images"][0]["metadata"]["sha256"] for row in data["runs"])
    assert len(export_evidence(project, history[0].run_id)["runs"]) == 1
    path = tmp_path / "import.json"
    item = evaluation(history[0])
    path.write_text(json.dumps([item.model_dump(mode="json")]))
    assert read_import(path) == [item]
    path.write_text(
        json.dumps({"format_version": 1, "evaluations": [item.model_dump(mode="json")]})
    )
    assert read_import(path) == [item]
    for value in (None, {}, {"format_version": 2, "evaluations": []}):
        path.write_text(json.dumps(value))
        with pytest.raises(KomaquillError):
            read_import(path)
    (project.runs / str(history[0].run_id) / "workflow-api.json").unlink()
    assert export_evidence(project, history[0].run_id)["runs"][0]["workflow_api"] is None


def test_cli_records_comparison_ai_attribution_and_exports(project, history, tmp_path):
    runner = CliRunner()

    def command(*args, expected=0):
        result = runner.invoke(
            app, ["--project", str(project.root), "evaluations", *map(str, args)]
        )
        assert result.exit_code == expected, result.output
        return result

    command(
        "add",
        history[0].run_id,
        "--decision",
        "accept",
        "--criterion",
        "composition",
        "--reason",
        "Clear foreground.",
    )
    instruction = tmp_path / "instructions.txt"
    instruction.write_text("Compare composition under the same criterion.")
    command(
        "compare",
        history[0].run_id,
        history[1].run_id,
        "--preference",
        "second",
        "--criterion",
        "composition",
        "--reason",
        "Better framing.",
        "--ai-model",
        "reviewer-model",
        "--model-version",
        "fixed-v1",
        "--instructions",
        instruction,
    )
    data = json.loads(command("list").stdout)
    assert len(data) == 2 and data[1]["evaluator"]["instruction_sha256"]
    command("list", "--run-id", history[1].run_id)
    output = tmp_path / "evidence.json"
    command("export", "--output", output)
    command("export", "--output", output, expected=1)
    command("import", output)
    command(
        "add",
        history[0].run_id,
        "--decision",
        "accept",
        "--criterion",
        "x",
        "--reason",
        "x",
        "--ai-model",
        "missing-instructions",
        expected=1,
    )
    assert len(select(project)) == 2


def test_instruction_snapshot_preserves_exact_utf8_and_rejects_naive_time(history):
    import hashlib
    from datetime import datetime

    instruction = "Assess framing.\r\nExplain the choice."
    reviewer = Evaluator(kind="ai", model="reviewer", instruction=instruction)
    assert reviewer.instruction_sha256 == hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    with pytest.raises(ValidationError):
        evaluation(history[0], created_at=datetime(2026, 1, 1))


def test_export_detects_missing_and_changed_images_without_rewriting_history(project, history):
    run = history[0]
    original = (project.runs / str(run.run_id) / "run.json").read_bytes()
    path = project.runs / str(run.run_id) / run.images[0]
    path.write_bytes(b"changed")
    image = export_evidence(project, run.run_id)["runs"][0]["images"][0]
    assert image["matches_record"] is False
    path.unlink()
    image = export_evidence(project, run.run_id)["runs"][0]["images"][0]
    assert image["observed"] is None
    assert (project.runs / str(run.run_id) / "run.json").read_bytes() == original


def test_import_rejects_broken_json_and_export_never_replaces_existing_file(tmp_path):
    from komaquill.infrastructure.evaluations import write_export

    path = tmp_path / "data.json"
    path.write_text("{")
    with pytest.raises(KomaquillError):
        read_import(path)
    with pytest.raises(KomaquillError):
        write_export(path, {"format_version": 1})
    assert path.read_text() == "{"
    assert not list(tmp_path.glob("*.tmp"))
