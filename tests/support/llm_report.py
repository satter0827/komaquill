"""Summarize retained comparison evidence without calling models or altering originals."""

import json
from collections import Counter, defaultdict


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(folder):
    result = read(folder / "results.json")
    manifest = read(folder / "original.json")["manifest"]
    cases = {case["id"]: case for case in manifest["cases"]}
    output = {
        "format_version": 1,
        "source_report": folder.name,
        "label_origin": manifest.get("label_origin"),
        "limitation": manifest.get("limitation"),
        "restoration": result["restoration"],
        "models": {},
    }
    for profile in ("lmstudio-4b", "lmstudio-9b"):
        rows = [row for row in result["rows"] if row["profile"] == profile]
        plans = [row for row in rows if row.get("kind") == "planning"]
        tokens = [row.get("tokens") for row in plans if row["status"] != "not_run"]
        summary = {
            "planning": {
                "recorded_conditions": len(plans),
                "saved": sum(row["status"] == "saved" for row in plans),
                "failed": sum(row["status"] == "failed" for row in plans),
                "not_run": sum(row["status"] == "not_run" for row in plans),
                "first_attempt_saved": sum(
                    row["status"] == "saved" and row.get("calls") == 1 for row in plans
                ),
                "calls": sum(row.get("calls", 0) for row in plans),
                "total_tokens": sum(tokens)
                if tokens and all(value is not None for value in tokens)
                else None,
                "unknown_token_conditions": sum(value is None for value in tokens),
                "total_seconds": sum(row.get("seconds", 0) for row in plans),
            },
            "vision": {
                "completed": 0,
                "failed": 0,
                "not_run": 0,
                "matching_checks": 0,
                "total_checks": 0,
                "unscored_checks": 0,
                "confusion": {},
            },
            "pairs": {},
            "memory": {},
        }
        confusion = Counter()
        order_choices = defaultdict(list)
        for row in rows:
            if row.get("kind") != "vision":
                continue
            case = cases[row["case_id"]]
            data = read(folder / row["record"]) if row.get("record") else {}
            parsed = data.get("parsed") if row["status"] == "completed" else None
            if case["task"] == "pair":
                choice = parsed["choice"] if parsed else row["status"]
                if row.get("reverse") and choice in {"first", "second"}:
                    choice = "second" if choice == "first" else "first"
                order_choices[case["id"]].append(choice)
                continue
            summary["vision"][row["status"]] += 1
            summary["vision"]["total_checks"] += len(case["criteria"])
            actual = {item["id"]: item["verdict"] for item in parsed["checks"]} if parsed else {}
            for criterion in case["criteria"]:
                expected = criterion["expected"]
                observed = actual.get(criterion["id"], "unscored")
                confusion[expected + "->" + observed] += 1
                summary["vision"]["matching_checks"] += observed == expected
                summary["vision"]["unscored_checks"] += observed == "unscored"
        summary["vision"]["confusion"] = dict(confusion)
        summary["pairs"] = {
            case: {
                "choices": choices,
                "order_consistent": choices[0] == choices[1]
                if len(choices) == 2
                and all(v in {"first", "second", "neither", "undecided"} for v in choices)
                else None,
            }
            for case, choices in order_choices.items()
        }
        output["models"][profile] = summary
    for line in (folder / "memory.jsonl").read_text(encoding="utf-8").splitlines():
        sample = json.loads(line)
        profile = sample["phase"].split(":")[0]
        if profile not in output["models"]:
            continue
        memory = output["models"][profile]["memory"]
        memory["observed_max_rss"] = max(
            memory.get("observed_max_rss", 0), sum(p["rss"] for p in sample.get("processes", []))
        )
        memory["min_system_available"] = min(
            memory.get("min_system_available", sample["available"]), sample["available"]
        )
        if "committed" in sample:
            memory["max_system_committed"] = max(
                memory.get("max_system_committed", 0), sample["committed"]
            )
    return output
