"""Keep suite limits, attribution and uncertain requests stable across restart."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.live import story_suite as suite
from tests.support.story_cases import cases


@pytest.fixture
def campaign(project):
    folder = project.root / "suite"
    suite.initialize(project.root, folder)
    record = suite.load(folder)
    record.gpu_started_at = datetime.now(UTC)
    suite.save(folder, record)
    return folder


def test_repeated_reservations_and_restart_do_not_reset_image_budget(campaign):
    for index in range(5):
        assert suite.reserve_operation(campaign, f"operation-{index}", 4)
    assert not suite.reserve_operation(campaign, "operation-0", 4)
    assert sum(suite.load(campaign).operations.values()) == 20
    with pytest.raises(RuntimeError):
        suite.reserve_operation(campaign, "overflow", 1)


def test_submission_deadline_survives_restart_and_prevents_reservation(campaign):
    value = suite.load(campaign)
    value.gpu_started_at -= timedelta(seconds=value.config.send_seconds)
    suite.save(campaign, value)
    with pytest.raises(RuntimeError):
        suite.reserve_operation(campaign, "late", 1)
    assert not suite.load(campaign).operations


def test_storage_limit_stops_new_requests_without_deleting_evidence(campaign):
    value = suite.load(campaign)
    value.config = value.config.model_copy(update={"storage_limit": 1})
    suite.save(campaign, value)
    with pytest.raises(RuntimeError):
        suite.reserve_operation(campaign, "large", 1)
    assert (campaign / "corpus.json").is_file()


def test_corpus_is_frozen_and_tampering_is_not_treated_as_a_new_suite(campaign):
    (campaign / "corpus.json").write_text("[]")
    with pytest.raises(ValueError):
        suite.load(campaign)


@pytest.mark.parametrize("fault", ["path", "duplicate", "negative", "overspend"])
def test_invalid_retained_identities_and_counters_are_rejected_before_use(campaign, fault):
    value = suite.load(campaign)
    if fault == "path":
        value.members = ["../unrelated"]
    elif fault == "duplicate":
        value.members = ["rainy-cat", "rainy-cat"]
    else:
        value.operations = {"invalid": -1 if fault == "negative" else value.config.output_limit + 1}
    suite.save(campaign, value)
    with pytest.raises(ValueError):
        suite.load(campaign)


def test_shutdown_prevents_new_work_and_keeps_concurrent_budget_updates(campaign, monkeypatch):
    from komaquill.infrastructure.atomic import atomic_json

    value = suite.load(campaign)
    value.members = ["rainy-cat"]
    suite.save(campaign, value)

    def stop(folder):
        latest = suite.load(campaign)
        latest.operations["already-accepted"] = 4
        suite.save(campaign, latest)
        atomic_json(folder / "shutdown.json", {"gpu": "stopped", "errors": []})

    monkeypatch.setattr(suite.story_completion, "stop", stop)
    suite.stop(campaign)
    assert suite.load(campaign).operations == {"already-accepted": 4}
    assert suite.load(campaign).shutdown_confirmed
    with pytest.raises(RuntimeError):
        suite.reserve_operation(campaign, "new", 1)


def test_real_measurement_adapter_counts_calls_and_restores_models_with_a_fake(
    project, monkeypatch
):
    from langchain_core.language_models.fake import FakeListLLM

    from komaquill.api import stories
    from komaquill.infrastructure.stories.llm import LangChainPlanner, planner

    folder = project.root / "measurements"
    suite.initialize(project.root, folder)

    def factory(context, selected, **kwargs):
        base = planner(
            context, selected.model_copy(update={"llm_profile": "fake"}), allow_real_llm=False
        )
        return LangChainPlanner(
            base.provenance,
            FakeListLLM(responses=[cases()["flying-hat"].draft.model_dump_json()], cache=False),
        )

    monkeypatch.setattr(stories, "planner", factory)
    monkeypatch.setattr(suite.llm_comparison, "inventory", lambda: [])
    monkeypatch.setattr(suite.llm_comparison, "switch", lambda *args: {"test": True})
    restored = []
    monkeypatch.setattr(suite.llm_comparison, "restore", lambda *args: restored.append(True) or [])
    suite.plan(folder, "flying-hat", project.root / "unused")
    state = suite.load(folder)
    assert state.planning_calls == 1 and len(restored) == 1
    assert state.plans["flying-hat"][0]["status"] == "saved"
    import json
    from uuid import UUID

    from komaquill.infrastructure.atomic import atomic_json
    from komaquill.infrastructure.configuration import Project
    from komaquill.infrastructure.stories.storage import FileStoryRepository

    repository = FileStoryRepository(Project(folder / "planning"))
    identity = UUID(state.plans["flying-hat"][0]["story_id"])
    first = repository.read(identity)
    assert first.request.purpose == "verification" and first.request.case_id == "flying-hat"
    record = json.loads((repository.folder(identity) / "plan.json").read_bytes())
    atomic_json(
        folder / "planning-reviews.json",
        [
            {
                "case_id": "flying-hat",
                "story_id": str(identity),
                "decision": "reject",
                "source_sha256": first.request.source_sha256,
                "plan_sha256": record["sha256"],
                "reason": "Do not add an observer to the empty park.",
                "evaluator": {
                    "kind": "ai",
                    "model": "fake-reviewer",
                    "instruction": "Check source fidelity.",
                },
            }
        ],
    )
    original = (folder / "planning/komaquill.toml").read_bytes()
    state.planning_started_at = datetime.now(UTC) - timedelta(
        seconds=state.config.planning_seconds - suite.RESTORE_SECONDS - 60
    )
    suite.save(folder, state)
    suite.plan(folder, "flying-hat", project.root / "unused")
    state = suite.load(folder)
    repaired = repository.read(UUID(state.plans["flying-hat"][-1]["story_id"]))
    assert state.planning_calls == 2 and len(restored) == 2
    assert "Do not add an observer" in repaired.planner.planning_policy["guidance"][-1]
    assert (folder / "planning/komaquill.toml").read_bytes() == original


def test_unknown_reservation_cannot_submit_a_fresh_baseline(campaign, monkeypatch):
    value = suite.load(campaign)
    value.members = ["rainy-cat"]
    value.operations = {"baseline-rainy-cat": 4}
    suite.save(campaign, value)
    monkeypatch.setattr(
        suite.story_completion,
        "load",
        lambda _: SimpleNamespace(scene_count=4, baseline_reserved=False),
    )
    submitted = []
    monkeypatch.setattr(suite.story_completion, "baseline", lambda _: submitted.append(True))
    with pytest.raises(RuntimeError):
        suite.baseline(campaign)
    assert not submitted


def test_global_conditioning_routes_prop_to_its_current_holder(project):
    from komaquill.application.stories.image_inputs import scene_input
    from komaquill.domain.stories import Scene, StoryPlan
    from tests.support.stories import planned

    _, original = planned(project)
    draft = cases()["red-book"].draft
    plan = original.model_copy(
        update={
            "characters": draft.characters,
            "props": draft.props,
            "scenes": [
                Scene(
                    **row.model_dump(),
                    scene_id=original.scenes[i].scene_id,
                    position=i + 1,
                    characters=[s.character for s in row.subjects],
                )
                for i, row in enumerate(draft.scenes)
            ],
        }
    )
    StoryPlan.model_validate(plan.model_dump())
    first = plan.scenes[0]
    profile = project.settings().presets["klein-4b"]
    before = scene_input(plan, first, profile)
    changed = first.model_copy(
        update={
            "props": [
                row.model_copy(update={"holder": "ren"}) if row.prop == "book" else row
                for row in first.props
            ]
        }
    )
    after = scene_input(plan, changed, profile)
    assert "red hardcover book" in before.subject_prompts[0]
    assert "red hardcover book" not in after.subject_prompts[0]
    assert "red hardcover book" in after.subject_prompts[1]
