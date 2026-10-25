"""Keep failed and incomplete measurements distinct from scored evidence."""

import json

from tests.support.llm_report import summarize


def test_comparison_summary_keeps_unknown_usage_and_counterbalances_pair_order(tmp_path):
    def write(name, value):
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")

    write(
        "original.json",
        {
            "manifest": {
                "cases": [
                    {
                        "id": "image",
                        "task": "single",
                        "criteria": [
                            {"id": "cat", "expected": "match"},
                            {"id": "coat", "expected": "mismatch"},
                        ],
                    },
                    {"id": "pair", "task": "pair"},
                ]
            }
        },
    )
    write(
        "image.json",
        {
            "parsed": {
                "checks": [{"id": "cat", "verdict": "match"}, {"id": "coat", "verdict": "unclear"}]
            }
        },
    )
    write("normal.json", {"parsed": {"choice": "first"}})
    write("reverse.json", {"parsed": {"choice": "second"}})
    rows = [
        {
            "kind": "planning",
            "profile": "lmstudio-4b",
            "status": "saved",
            "calls": 1,
            "tokens": 10,
            "seconds": 2,
        },
        {
            "kind": "planning",
            "profile": "lmstudio-4b",
            "status": "failed",
            "calls": 1,
            "tokens": None,
            "seconds": 3,
        },
        {"profile": "lmstudio-9b", "status": "not_run", "reason": "budget_exhausted"},
        {
            "kind": "vision",
            "profile": "lmstudio-4b",
            "case_id": "image",
            "status": "completed",
            "record": "image.json",
        },
        {
            "kind": "vision",
            "profile": "lmstudio-4b",
            "case_id": "pair",
            "status": "completed",
            "reverse": False,
            "record": "normal.json",
        },
        {
            "kind": "vision",
            "profile": "lmstudio-4b",
            "case_id": "pair",
            "status": "completed",
            "reverse": True,
            "record": "reverse.json",
        },
        {"kind": "vision", "profile": "lmstudio-9b", "case_id": "image", "status": "not_run"},
        {"kind": "vision", "profile": "lmstudio-9b", "case_id": "pair", "status": "failed"},
        {"kind": "vision", "profile": "lmstudio-9b", "case_id": "pair", "status": "not_run"},
    ]
    write("results.json", {"restoration": "verified", "rows": rows})
    (tmp_path / "memory.jsonl").write_text(
        json.dumps(
            {
                "phase": "lmstudio-4b:vision",
                "available": 20,
                "committed": 100,
                "processes": [{"rss": 30}],
            }
        ),
        encoding="utf-8",
    )
    result = summarize(tmp_path)["models"]
    small = result["lmstudio-4b"]
    assert small["planning"]["total_tokens"] is None
    assert small["planning"]["calls"] == 2
    assert small["planning"]["unknown_token_conditions"] == 1
    assert small["vision"]["matching_checks"] == 1
    assert small["vision"]["confusion"] == {"match->match": 1, "mismatch->unclear": 1}
    assert small["pairs"]["pair"]["order_consistent"] is True
    assert small["memory"]["observed_max_rss"] == 30
    large = result["lmstudio-9b"]
    assert large["planning"]["total_tokens"] is None
    assert large["vision"]["not_run"] == 1 and large["vision"]["failed"] == 0
    assert large["vision"]["unscored_checks"] == 2
    assert large["pairs"]["pair"]["order_consistent"] is None
