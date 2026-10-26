"""Reject visual approval when the evaluator omits required evidence."""

from uuid import uuid4

import pytest

from tests.live.completion_report import REQUIRED_CHECKS, Review


def test_approval_cannot_skip_source_and_continuity_checks():
    with pytest.raises(ValueError):
        Review(
            scene=1,
            run_id=uuid4(),
            image="images/0001.png",
            decision="accept",
            reason="Only appearance was checked.",
            checks=[{"criterion": "appearance", "verdict": "match", "reason": "Clothing matches."}],
        )


@pytest.mark.parametrize(
    "case", ["complete", "duplicate", "unknown", "unclear", "reject", "undecided"]
)
def test_review_completeness_and_acceptance_are_independent(case):
    checks = [
        {"criterion": key, "verdict": "match", "reason": "Observed."} for key in REQUIRED_CHECKS
    ]
    decision = "accept"
    if case == "duplicate":
        checks[-1]["criterion"] = checks[0]["criterion"]
    elif case == "unknown":
        checks[-1]["criterion"] = "unrelated"
    elif case == "unclear":
        checks[0]["verdict"] = "unclear"
    elif case in {"reject", "undecided"}:
        decision = case
        checks.pop()
    payload = {
        "scene": 1,
        "run_id": uuid4(),
        "image": "images/0001.png",
        "decision": decision,
        "reason": "Check every condition before approval.",
        "checks": checks,
    }
    if case == "complete":
        assert Review.model_validate(payload).decision == "accept"
    else:
        with pytest.raises(ValueError):
            Review.model_validate(payload)
