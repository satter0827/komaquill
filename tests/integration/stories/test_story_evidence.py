"""Check summary/full parity, resolvable files and complete rejected evidence."""

import hashlib
import json
from contextlib import contextmanager
from importlib.resources import files
from uuid import UUID

import pytest
from langchain_core.language_models.fake import FakeListLLM

from komaquill.api import Client, KomaquillError, StoryEvidence, stories
from komaquill.infrastructure.stories.configuration import settings
from komaquill.infrastructure.stories.llm import LangChainPlanner, planner
from tests.support.stories import source


def expand(value, objects):
    if isinstance(value, dict):
        if set(value) == {"object_ref"}:
            return expand(objects[value["object_ref"]], objects)
        return {key: expand(item, objects) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, objects) for item in value]
    return value


def test_summary_and_full_share_selection_conditions_and_original_bytes(
    project, session, client, monkeypatch
):
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connection(_):
        yield client, active

    monkeypatch.setattr(stories, "connected", connection)
    api = Client(project.root)
    snapshot = list(api.stories.generate(source(), images=1, llm_profile="fake", seed=77))[
        -1
    ].snapshot
    summary = api.stories.export(snapshot.story_id)
    full = api.stories.export(snapshot.story_id, detail="full")
    assert isinstance(summary, StoryEvidence) and summary.detail == "summary"
    assert summary.objects == {} and full.objects
    assert summary.source == full.source and summary.render == full.render
    assert summary.planning == full.planning
    assert summary.selected_run_ids == [snapshot.scenes[0].run_id]
    assert list(summary.render.seeds.values()) == [77]
    assert summary.planning.status == "succeeded"
    assert summary.planning.calls == 1
    assert summary.planning.usage.total_tokens is None
    assert "total_tokens" in summary.planning.unknown_fields
    image = summary.runs[0].images[0]
    target = project.root / image.workspace_path
    assert target == api.artifact_path(image.key)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == image.metadata.sha256
    assert image.matches_record and image.observed.valid
    for record in full.records:
        raw = (project.root / record.workspace_path).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record.sha256
        assert expand(full.objects[record.object_ref], full.objects) == json.loads(raw)
    assert all(
        not (PathValue.startswith("/") or ":" in PathValue)
        for PathValue in [row.workspace_path for row in full.records]
    )


def test_failed_repair_records_final_reason_and_deduplicates_schemas(project, monkeypatch):
    base = planner(project, settings(project, llm_profile="fake"), allow_real_llm=False)
    good = json.loads(
        files("komaquill")
        .joinpath("resources/stories/examples/rainy-cat/plan-4.json")
        .read_text(encoding="utf-8")
    )
    good["scenes"][1]["narrative"] = good["scenes"][0]["narrative"]
    fake = LangChainPlanner(base.provenance, FakeListLLM(responses=[json.dumps(good)]))
    monkeypatch.setattr(stories, "planner", lambda *a, **k: fake)
    api = Client(project.root)
    with pytest.raises(KomaquillError) as failure:
        api.stories.plan(source(), images=4, llm_profile="fake")
    identity = UUID(failure.value.details["story_id"])
    evidence = api.stories.export(identity, detail="full")
    assert evidence.story is None and evidence.planning.status == "rejected"
    assert evidence.planning.calls == 2 and evidence.planning.repair_limit_exhausted
    assert not evidence.planning.failure.retryable
    assert all(
        row.validation_issues[0].code == "duplicate_narrative" for row in evidence.planning.attempts
    )
    from komaquill.infrastructure.stories.evidence import object_hash

    raw = api.stories._repository.planning_evidence(identity)
    schema_id = object_hash(raw["inputs"][0]["schema"])
    assert schema_id in evidence.objects
    assert sum(1 for k in evidence.objects if k == schema_id) == 1


def test_old_unknown_outcomes_are_not_invented_and_exports_do_not_modify_records(project):
    api = Client(project.root)
    with pytest.raises(KomaquillError) as error:
        api.stories.plan(source() + " changed", llm_profile="fake")
    identity = UUID(error.value.details["story_id"])
    root = api.stories._repository.folder(identity)
    (root / "planning-result.json").unlink()
    for path in (root / "planning").glob("[0-9]*.json"):
        row = json.loads(path.read_bytes())
        row.pop("validation_issues", None)
        path.write_text(json.dumps(row), encoding="utf-8")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    evidence = api.stories.export(identity)
    assert evidence.planning.status == "unknown"
    assert evidence.planning.elapsed_seconds is None
    assert evidence.planning.attempts[0].validation_issues is None
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_interrupted_call_has_a_separate_outcome(project, monkeypatch):
    options = settings(project, llm_profile="fake")
    base = planner(project, options, allow_real_llm=False)
    monkeypatch.setattr(base, "complete", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(stories, "planner", lambda *a, **k: base)
    api = Client(project.root)
    with pytest.raises(KeyboardInterrupt) as result:
        api.stories.plan(source(), llm_profile="fake")
    evidence = api.stories.export(result.value.story_id)
    assert evidence.planning.status == "interrupted" and evidence.planning.calls == 1
    assert evidence.story is None


def test_legacy_image_observations_do_not_backfill_history(project, session, client, monkeypatch):
    active = session.model_copy(update={"settings": project.settings()})

    @contextmanager
    def connection(_):
        yield client, active

    monkeypatch.setattr(stories, "connected", connection)
    api = Client(project.root)
    snapshot = list(api.stories.generate(source(), images=1, llm_profile="fake"))[-1].snapshot
    run_id = snapshot.scenes[0].run_id
    path = api.artifact_path(f"runs/{run_id}/run.json")
    raw = json.loads(path.read_bytes())
    raw.pop("image_metadata")
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    evidence = api.stories.export(snapshot.story_id)
    image = evidence.runs[0].images[0]
    assert image.metadata is None and image.matches_record is None and image.observed.valid
    assert path.read_bytes() == before


def test_reported_usage_is_aggregated_without_replacing_unknowns(project, monkeypatch):
    from komaquill.domain.stories import PlanningResponse, TokenUsage

    base = planner(project, settings(project, llm_profile="fake"), allow_real_llm=False)
    response = (
        files("komaquill")
        .joinpath("resources/stories/examples/rainy-cat/plan-4.json")
        .read_text(encoding="utf-8")
    )
    monkeypatch.setattr(
        base,
        "complete",
        lambda *a: PlanningResponse(
            content=response,
            model="fake-list",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=10, output_tokens=20, total_tokens=30),
        ),
    )
    monkeypatch.setattr(stories, "planner", lambda *a, **k: base)
    api = Client(project.root)
    plan = api.stories.plan(source(), images=4, llm_profile="fake")
    evidence = api.stories.export(plan.request.story_id)
    assert evidence.planning.usage.total_tokens == 30
    assert evidence.planning.usage.reasoning_tokens is None
    assert evidence.planning.producer == plan.planner.app_identity
    assert "total_tokens" not in evidence.planning.unknown_fields
