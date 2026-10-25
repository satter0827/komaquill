"""Exercise comparison decisions with FakeListLLM and local test boundaries."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake import FakeListLLM

from komaquill.infrastructure.stories.configuration import settings
from tests.support import llm_comparison as comparison


def test_9b_is_selectable_without_changing_the_default(project, monkeypatch):
    assert settings(project).llm_profile == "lmstudio-4b"
    selected = settings(project, llm_profile="lmstudio-9b")
    profile = selected.llm_profiles[selected.llm_profile]
    assert profile.model == "qwen3.5-9b-uncensored-hauhaucs-aggressive"
    assert profile.provider == "openai-compatible" and profile.require_model_match
    monkeypatch.setenv("KOMAQUILL_STORY_LLM_PROFILE", "lmstudio-9b")
    assert settings(project).llm_profile == "lmstudio-9b"
    assert settings(project, llm_profile="fake").llm_profile == "fake"
    assert "llm_profiles" not in project.settings().model_dump()


@pytest.mark.parametrize("verdict", ["match", "mismatch", "unclear"])
def test_fake_assessment_preserves_uncertainty_without_real_inference(verdict):
    answer = {
        "checks": [{"id": "cat", "verdict": verdict}],
        "decision": "undecided",
        "reason": "Visible evidence",
    }
    fake = FakeListLLM(responses=[json.dumps(answer)], cache=False)
    result = comparison.assessment(
        fake,
        [{"role": "user", "content": "Inspect this test image"}],
        comparison.ImageAssessment,
        ["cat"],
    )
    assert result["parsed"] == answer and result["usage"] is None


@pytest.mark.parametrize(
    "checks", [[], [{"id": "other", "verdict": "match"}], [{"id": "cat", "verdict": "match"}] * 2]
)
def test_invalid_or_duplicate_assessment_ids_are_rejected(checks):
    fake = FakeListLLM(
        responses=[json.dumps({"checks": checks, "decision": "reject", "reason": "Test"})]
    )
    assert (
        comparison.assessment(
            fake, [{"role": "user", "content": "Test"}], comparison.ImageAssessment, ["cat"]
        )["failure"]
        == "invalid_assessment"
    )


@pytest.mark.parametrize(
    "text", ["not json", '{"choice":"first"}', '{"choice":"third","reason":"Test"}']
)
def test_invalid_pair_output_is_not_repaired_or_adopted(text):
    fake = FakeListLLM(responses=[text, "unexpected second invocation"])
    assert (
        comparison.assessment(
            fake, [{"role": "user", "content": "Test"}], comparison.PairAssessment
        )["failure"]
        == "invalid_assessment"
    )
    assert fake.i == 1


@pytest.mark.parametrize("choice", ["first", "second", "neither", "undecided"])
def test_pair_selection_allows_both_rejected_or_undecided(choice):
    fake = FakeListLLM(responses=[json.dumps({"choice": choice, "reason": "Test"})])
    assert (
        comparison.assessment(
            fake, [{"role": "user", "content": "Test"}], comparison.PairAssessment
        )["parsed"]["choice"]
        == choice
    )


@pytest.mark.parametrize("available, expected", [(199, False), (200, True), (201, True)])
def test_commit_guard_accounts_for_the_reserve(monkeypatch, available, expected):
    monkeypatch.setattr(comparison, "COMMIT_RESERVE", 100)
    assert comparison.has_capacity({"available": 0, "commit_available": available}, 100) is expected


def test_image_prompt_does_not_reveal_reference_labels_or_accept_paths_outside_data(tmp_path):
    path = tmp_path / ".artifacts/data/example.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"retained image bytes")
    case = {
        "task": "single",
        "scene": "One cat",
        "image": ".artifacts/data/example.png",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "criteria": [{"id": "cat", "statement": "One cat is visible", "expected": "mismatch"}],
    }
    messages = comparison.image_messages(case, tmp_path)
    assert "expected" not in messages[0]["content"][0]["text"]
    case["image"] = "outside.png"
    with pytest.raises(ValueError):
        comparison.image_messages(case, tmp_path)


def test_pair_order_is_reversed_without_changing_the_criteria(tmp_path):
    folder = tmp_path / ".artifacts/data"
    folder.mkdir(parents=True)
    (folder / "first.png").write_bytes(b"first")
    (folder / "second.png").write_bytes(b"second")
    case = {
        "task": "pair",
        "scene": "One cat",
        "images": [".artifacts/data/first.png", ".artifacts/data/second.png"],
        "criteria": [],
        "sha256": {
            f".artifacts/data/{name}.png": hashlib.sha256(name.encode()).hexdigest()
            for name in ("first", "second")
        },
    }
    normal = comparison.image_messages(case, tmp_path)
    reversed_case = comparison.image_messages(case, tmp_path, True)
    assert normal[0]["content"][1:] == list(reversed(reversed_case[0]["content"][1:]))
    assert normal[0]["content"][0] == reversed_case[0]["content"][0]


def test_switch_does_not_unload_an_unexpected_model(monkeypatch, tmp_path):
    monkeypatch.setattr(
        comparison,
        "inventory",
        lambda: [{"type": "llm", "key": "other", "loaded_instances": [{"id": "other"}]}],
    )
    control = Mock()
    monkeypatch.setattr(comparison, "run_control", control)
    with pytest.raises(RuntimeError, match="unexpected_loaded_model"):
        comparison.switch("lms", "9b", 8192, {"4b", "9b"}, tmp_path)
    control.assert_not_called()


def test_switch_checks_memory_after_unloading_and_never_loads_if_insufficient(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(comparison, "inventory", lambda: [])
    monkeypatch.setattr(comparison, "estimate", lambda *args: 100)
    monkeypatch.setattr(comparison, "memory", lambda: {"available": 0, "commit_available": 0})
    control = Mock()
    monkeypatch.setattr(comparison, "run_control", control)
    with pytest.raises(MemoryError):
        comparison.switch("lms", "9b", 8192, {"4b", "9b"}, tmp_path)
    control.assert_not_called()


def test_output_truncation_is_not_treated_as_a_selection():
    model = Mock()
    model.bind.return_value.invoke.return_value = SimpleNamespace(
        content='{"choice":"first","reason":"test"}',
        response_metadata={"finish_reason": "length"},
        usage_metadata=None,
    )
    assert (
        comparison.assessment(model, [], comparison.PairAssessment)["failure"] == "output_truncated"
    )


def test_blinded_content_keeps_raw_rejected_answers_without_model_metadata():
    raw = {
        "objects": {"a": {"response": {"content": "rejected answer", "model": "secret-name"}}},
        "planning": {"producer": {"model": "secret-name"}},
        "source": {"text": "story"},
        "scenes": [],
    }
    blind = comparison.blind_content(raw)
    assert blind["responses"] == ["rejected answer"]
    assert "secret-name" not in json.dumps(blind)


def test_restore_uses_original_parallelism_and_verifies_the_effective_settings(
    monkeypatch, tmp_path
):
    original = [{"key": "4b", "id": "4b", "config": {"context_length": 8192, "parallel": 4}}]
    responses = iter(
        [
            [{"type": "llm", "key": "9b", "loaded_instances": [{"id": "9b"}]}],
            [
                {
                    "type": "llm",
                    "key": "4b",
                    "loaded_instances": [{"id": "4b", "config": original[0]["config"]}],
                }
            ],
        ]
    )
    monkeypatch.setattr(comparison, "inventory", lambda: next(responses))
    control = Mock()
    monkeypatch.setattr(comparison, "run_control", control)
    assert comparison.restore("lms", original, {"4b", "9b"}, tmp_path) == original
    assert control.call_args_list[0].args[1] == ["unload", "9b"]
    assert control.call_args_list[1].args[1][-5:] == ["--parallel", "4", "--gpu", "off", "--yes"]


def test_restore_reports_mismatch_instead_of_claiming_success(monkeypatch, tmp_path):
    monkeypatch.setattr(comparison, "inventory", lambda: [])
    monkeypatch.setattr(comparison, "run_control", Mock())
    with pytest.raises(RuntimeError, match="restoration_mismatch"):
        comparison.restore(
            "lms",
            [{"key": "4b", "id": "4b", "config": {"context_length": 8192, "parallel": 4}}],
            {"4b"},
            tmp_path,
        )


@pytest.mark.parametrize("interrupted", [False, True])
def test_runner_always_restores_models_when_loading_fails(
    project, tmp_path, monkeypatch, interrupted
):
    from contextlib import nullcontext

    from tests.live import llm_comparison as runner

    monkeypatch.setattr(runner, "inventory", lambda: [])
    monkeypatch.setattr(runner, "runtime_observation", lambda: {})
    monkeypatch.setattr(runner, "MemoryTrace", lambda path: nullcontext(SimpleNamespace(phase="")))
    fail = KeyboardInterrupt() if interrupted else MemoryError()
    monkeypatch.setattr(runner, "switch", Mock(side_effect=fail))
    restored = Mock(return_value=[])
    monkeypatch.setattr(runner, "restore", restored)
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"cases":[]}', encoding="utf-8")
    report = tmp_path / "report"
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            runner.run(project.root, report, manifest, "lms")
    else:
        assert runner.run(project.root, report, manifest, "lms")["restoration"] == "verified"
    restored.assert_called_once()
    assert (
        json.loads((report / "results.json").read_text(encoding="utf-8"))["restoration"]
        == "verified"
    )


def test_runner_requires_explicit_real_model_flag(monkeypatch):
    from tests.live import llm_comparison as runner

    monkeypatch.setattr(
        "sys.argv", ["llm_comparison", "--project", "test", "--manifest", "x", "--report", "y"]
    )
    unexpected = Mock()
    monkeypatch.setattr(runner, "run", unexpected)
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2
    unexpected.assert_not_called()


def test_estimate_reads_stderr_without_recording_raw_console_content(monkeypatch, tmp_path):
    monkeypatch.setattr(
        comparison.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="\n", stderr="Estimated Total Memory: 7.16 GiB\nprivate diagnostic"
        ),
    )
    value = comparison.estimate("lms", "9b", 8192, tmp_path)
    assert value == int(7.16 * 1024**3)
    saved = (tmp_path / "control.jsonl").read_text(encoding="utf-8")
    assert "private diagnostic" not in saved
    assert "7.16 GiB" in saved


def test_image_hash_mismatch_stops_before_request(tmp_path):
    folder = tmp_path / ".artifacts/data"
    folder.mkdir(parents=True)
    (folder / "image.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="manifest"):
        comparison.image_messages(
            {
                "task": "single",
                "scene": "test",
                "criteria": [],
                "image": ".artifacts/data/image.png",
                "sha256": "0" * 64,
            },
            tmp_path,
        )


def test_blind_review_resolves_deduplicated_raw_responses():
    value = comparison.blind_content(
        {
            "objects": {
                "attempt": {"response": {"content": {"object_ref": "body"}, "model": "hidden"}},
                "body": "visible rejected answer",
            }
        }
    )
    assert value["responses"] == ["visible rejected answer"]
