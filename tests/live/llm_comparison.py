"""Run a bounded, explicitly authorized local 4B/9B comparison and restore models."""

import argparse
import hashlib
import json
import os
import random
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from komaquill.api import Client, KomaquillError
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.stories.configuration import settings
from tests.support.llm_comparison import (
    ENDPOINT,
    ImageAssessment,
    MemoryTrace,
    PairAssessment,
    assessment,
    blind_content,
    image_messages,
    inventory,
    loaded,
    restore,
    runtime_observation,
    switch,
)
from tests.support.stories import source
from tests.support.story_evidence import CASES, planning_measurement

RESTORE_RESERVE = 180


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def run(root, report, manifest, executable, minutes=90):
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    if not 0 < minutes <= 90:
        raise ValueError("The measurement budget must be between zero and ninety minutes")
    project = Project(root)
    original = loaded(inventory())
    original_engine = runtime_observation()
    if original and (
        not original_engine.get("processes")
        or any(process.get("gpu_layers") != 0 for process in original_engine["processes"])
    ):
        raise RuntimeError("original_cpu_execution_unconfirmed")
    profiles = settings(project).llm_profiles
    names = ("lmstudio-4b", "lmstudio-9b")
    if any(profiles[name].base_url != ENDPOINT + "/v1" for name in names):
        raise ValueError("Comparison profiles must use the local LM Studio endpoint")
    allowed = {profiles[name].model for name in names}
    if any(row["key"] not in allowed for row in original):
        raise RuntimeError("unexpected_loaded_model")
    cases = json.loads(manifest.read_text(encoding="utf-8"))
    fixed_hashes = {
        case["image"]: case["sha256"] for case in cases["cases"] if case["task"] == "single"
    }
    for case in cases["cases"]:
        if case["task"] == "pair" and "sha256" not in case:
            case["sha256"] = {image: fixed_hashes.get(image) for image in case["images"]}
    report.mkdir(parents=True, exist_ok=False)
    write(
        report / "original.json",
        {
            "models": original,
            "engine": original_engine,
            "identity": project.identity(),
            "manifest": cases,
            "started_at": datetime.now(UTC).isoformat(),
        },
    )
    workspace = report / "workspace"
    workspace.mkdir()
    (workspace / "komaquill.toml").write_text(
        '[story]\nimage_profile="klein-4b"\n', encoding="utf-8"
    )
    api = Client(workspace)
    deadline = time.monotonic() + minutes * 60
    rows = []
    result = {
        "format_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "identity": project.identity(),
        "budget_minutes": minutes,
        "rows": rows,
        "loads": [],
        "restoration": "pending",
    }

    def checkpoint():
        write(report / "results.json", result)

    def available(seconds):
        return time.monotonic() + seconds + RESTORE_RESERVE < deadline

    checkpoint()
    with MemoryTrace(report / "memory.jsonl") as trace:
        try:
            for name in names:
                profile = profiles[name]
                trace.phase = name + ":load"
                context = 8192
                if not available(180):
                    rows.append(
                        {"profile": name, "status": "not_run", "reason": "budget_exhausted"}
                    )
                    break
                try:
                    load = switch(executable, profile.model, context, allowed, report)
                except (MemoryError, RuntimeError, subprocess.TimeoutExpired) as error:
                    result["loads"].append(
                        {"profile": name, "context": context, "failure": type(error).__name__}
                    )
                    if name != "lmstudio-9b":
                        checkpoint()
                        continue
                    context = 4096
                    try:
                        load = switch(executable, profile.model, context, allowed, report)
                    except Exception as fallback_error:
                        result["loads"].append(
                            {
                                "profile": name,
                                "context": context,
                                "failure": type(fallback_error).__name__,
                            }
                        )
                        checkpoint()
                        continue
                load["engine"] = runtime_observation()
                if any(p.get("gpu_layers") != 0 for p in load["engine"].get("processes", [])):
                    raise RuntimeError("cpu_execution_unconfirmed")
                result["loads"].append({"profile": name, "context": context, **load})
                checkpoint()
                if context == 8192:
                    for case_id, text in CASES.items():
                        text = source() if text is None else text
                        for count in (4, "auto"):
                            if not available(600):
                                rows.append(
                                    {
                                        "kind": "planning",
                                        "profile": name,
                                        "case_id": case_id,
                                        "count": count,
                                        "status": "not_run",
                                        "reason": "budget_exhausted",
                                    }
                                )
                                continue
                            trace.phase = f"{name}:planning:{case_id}:{count}"
                            os.environ.update(
                                KOMAQUILL_STORY_PURPOSE="verification",
                                KOMAQUILL_STORY_CASE_ID=case_id,
                            )
                            start_at = datetime.now(UTC).isoformat()
                            before = time.monotonic()
                            plan = failure = story_id = None
                            try:
                                plan = api.stories.plan(
                                    text,
                                    source_name=case_id + ".txt",
                                    images=count,
                                    llm_profile=name,
                                    image_profile="klein-4b",
                                    allow_real_llm=True,
                                )
                                story_id = plan.request.story_id
                            except KomaquillError as error:
                                failure = error
                                if "story_id" in error.details:
                                    story_id = UUID(error.details["story_id"])
                            record = planning_measurement(
                                api,
                                story_id,
                                case_id=case_id,
                                count=count,
                                source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                                started_at=start_at,
                                elapsed_seconds=time.monotonic() - before,
                                failure=failure,
                            )
                            key = f"{name}-{case_id}-{count}"
                            record["full_evidence"] = (
                                api.stories.export(story_id, detail="full").model_dump(mode="json")
                                if story_id
                                else None
                            )
                            write(report / (key + ".json"), record)
                            rows.append(
                                {
                                    "kind": "planning",
                                    "profile": name,
                                    "case_id": case_id,
                                    "count": count,
                                    "status": record["status"],
                                    "seconds": record["elapsed_seconds"],
                                    "calls": record["calls"],
                                    "tokens": record["total_tokens"],
                                    "record": key + ".json",
                                }
                            )
                            checkpoint()
                            print(json.dumps(rows[-1]), flush=True)
                else:
                    rows.append(
                        {
                            "kind": "planning",
                            "profile": name,
                            "status": "not_run",
                            "reason": "context_8192_unavailable",
                        }
                    )
                model = ChatOpenAI(
                    model=profile.model,
                    base_url=profile.base_url,
                    api_key=SecretStr("lm-studio"),
                    temperature=0.3,
                    max_completion_tokens=512,
                    timeout=120,
                    max_retries=0,
                    reasoning_effort="none",
                )
                for case in cases["cases"]:
                    orders = (False, True) if case["task"] == "pair" else (False,)
                    for reverse in orders:
                        if not available(120):
                            rows.append(
                                {
                                    "kind": "vision",
                                    "profile": name,
                                    "case_id": case["id"],
                                    "reverse": reverse,
                                    "status": "not_run",
                                    "reason": "budget_exhausted",
                                }
                            )
                            continue
                        trace.phase = f"{name}:vision:{case['id']}:{reverse}"
                        messages = image_messages(case, root, reverse)
                        key = f"{name}-{case['id']}-{int(reverse)}"
                        write(
                            report / (key + ".input.json"),
                            {
                                "model": profile.model,
                                "context": context,
                                "temperature": 0.3,
                                "reasoning_effort": "none",
                                "max_output_tokens": 512,
                                "timeout_seconds": 120,
                                "text": messages[0]["content"][0]["text"],
                                "images": list(reversed(case.get("images", [case.get("image")])))
                                if reverse
                                else case.get("images", [case.get("image")]),
                                "messages_sha256": hashlib.sha256(
                                    json.dumps(
                                        messages, ensure_ascii=False, sort_keys=True
                                    ).encode()
                                ).hexdigest(),
                                "schema": (
                                    PairAssessment if case["task"] == "pair" else ImageAssessment
                                ).model_json_schema(),
                            },
                        )
                        before = time.monotonic()
                        try:
                            response = assessment(
                                model,
                                messages,
                                PairAssessment if case["task"] == "pair" else ImageAssessment,
                                [item["id"] for item in case["criteria"]],
                            )
                            actual = response.get("metadata", {}).get("model_name") or response.get(
                                "metadata", {}
                            ).get("model")
                            if actual != profile.model:
                                response["failure"] = "model_identity_unconfirmed"
                        except Exception as error:
                            response = {"failure": type(error).__name__}
                        response.update(seconds=time.monotonic() - before)
                        write(report / (key + ".json"), response)
                        rows.append(
                            {
                                "kind": "vision",
                                "profile": name,
                                "case_id": case["id"],
                                "reverse": reverse,
                                "status": "failed" if "failure" in response else "completed",
                                "seconds": response["seconds"],
                                "record": key + ".json",
                            }
                        )
                        checkpoint()
                        print(json.dumps(rows[-1]), flush=True)
        except BaseException as error:
            result["interruption"] = {"type": type(error).__name__}
            checkpoint()
            raise
        finally:
            trace.phase = "restore"
            try:
                result["restored_models"] = restore(executable, original, allowed, report)
                result["restored_engine"] = runtime_observation()
                result["restoration"] = "verified"
            except Exception as error:
                result["restoration"] = "failed"
                result["restoration_error"] = {"type": type(error).__name__}
            result["finished_at"] = datetime.now(UTC).isoformat()
            checkpoint()
    blind = []
    for row in rows:
        if row.get("kind") != "planning" or "record" not in row:
            continue
        record = json.loads((report / row["record"]).read_text(encoding="utf-8"))
        content = record.get("full_evidence")
        if content:
            blind.append(
                {
                    "blind_id": uuid4().hex[:10],
                    "case_id": row["case_id"],
                    "count": row["count"],
                    **blind_content(content),
                    "_record": row["record"],
                }
            )
    random.SystemRandom().shuffle(blind)
    write(report / "blind-map.json", {item["blind_id"]: item.pop("_record") for item in blind})
    write(report / "blind-plans.json", blind)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-llm", action="store_true")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--lms", type=Path, default=Path.home() / ".lmstudio/bin/lms.exe")
    parser.add_argument("--minutes", type=float, default=90)
    args = parser.parse_args()
    if not args.allow_real_llm:
        parser.error("Explicit --allow-real-llm is required")
    result = run(
        args.project.resolve(),
        args.report.resolve(),
        args.manifest.resolve(),
        args.lms,
        args.minutes,
    )
    print(
        json.dumps({"report": str(args.report), "restoration": result["restoration"]}), flush=True
    )
    return 0 if result["restoration"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
