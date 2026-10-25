"""Measure explicit local model comparisons independently of routine quality gates."""

import base64
import ctypes
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from contextlib import AbstractContextManager
from typing import Literal

import httpx
import psutil
from pydantic import BaseModel, ConfigDict

ENDPOINT = "http://127.0.0.1:1234"
COMMIT_RESERVE = 1024**3
CONTROL_TIMEOUT = 180


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    verdict: Literal["match", "mismatch", "unclear"]


class ImageAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checks: list[CheckResult]
    decision: Literal["accept", "reject", "undecided"]
    reason: str


class PairAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: Literal["first", "second", "neither", "undecided"]
    reason: str


def inventory():
    with httpx.Client(trust_env=False, timeout=10) as client:
        response = client.get(ENDPOINT + "/api/v1/models")
        response.raise_for_status()
        return response.json()["models"]


def loaded(models):
    return [
        {"key": row["key"], **instance}
        for row in models
        if row["type"] == "llm"
        for instance in row.get("loaded_instances", [])
    ]


def memory():
    ram = psutil.virtual_memory()
    row = {"available": ram.available, "total": ram.total}
    if os.name == "nt":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in (
                    "total_physical",
                    "available_physical",
                    "total_page",
                    "available_page",
                    "total_virtual",
                    "available_virtual",
                    "extended",
                )
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("Memory observation failed")
        row.update(
            commit_limit=status.total_page,
            commit_available=status.available_page,
            committed=status.total_page - status.available_page,
        )
    return row


def has_capacity(observation, estimated_bytes):
    return (
        observation.get("commit_available", observation["available"])
        >= estimated_bytes + COMMIT_RESERVE
    )


class MemoryTrace(AbstractContextManager):
    def __init__(self, path):
        self.path = path
        self.stop = threading.Event()
        self.phase = "setup"
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        with self.path.open("a", encoding="utf-8") as stream:
            while not self.stop.is_set():
                try:
                    sample = {"at": time.time(), "phase": self.phase, **memory()}
                    sample["processes"] = [
                        {
                            "pid": p.pid,
                            "rss": p.memory_info().rss,
                            "cpu_seconds": sum(p.cpu_times()[:2]),
                        }
                        for p in psutil.process_iter(["name"])
                        if (p.info["name"] or "").lower() == "llama-server.exe"
                    ]
                    stream.write(json.dumps(sample) + "\n")
                    stream.flush()
                except (OSError, psutil.Error):
                    pass
                self.stop.wait(1)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=3)


def run_control(executable, arguments, folder):
    result = subprocess.run(
        [str(executable), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CONTROL_TIMEOUT,
    )
    with (folder / "control.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "at": time.time(),
                    "arguments": arguments,
                    "exit_code": result.returncode,
                    "estimate_lines": re.findall(
                        r"Estimated (?:GPU|Total) Memory:[^\r\n]*",
                        result.stdout + result.stderr,
                    ),
                }
            )
            + "\n"
        )
    if result.returncode:
        raise RuntimeError("model_control_failed")
    return result.stdout + result.stderr


def estimate(executable, model, context, folder):
    output = run_control(
        executable,
        [
            "load",
            model,
            "--estimate-only",
            "--gpu",
            "off",
            "--context-length",
            str(context),
            "--parallel",
            "1",
        ],
        folder,
    )
    found = re.search(r"Estimated Total Memory:\s+([0-9.]+)\s+(GiB|MiB)", output)
    if not found:
        raise RuntimeError("estimate_unavailable")
    return int(float(found[1]) * (1024**3 if found[2] == "GiB" else 1024**2))


def switch(executable, model, context, allowed, folder):
    current = loaded(inventory())
    if any(row["key"] not in allowed for row in current):
        raise RuntimeError("unexpected_loaded_model")
    for row in current:
        run_control(executable, ["unload", row["id"]], folder)
    required = estimate(executable, model, context, folder)
    observed = memory()
    if not has_capacity(observed, required):
        raise MemoryError("insufficient_commit_reserve")
    started = time.monotonic()
    run_control(
        executable,
        [
            "load",
            model,
            "--identifier",
            model,
            "--context-length",
            str(context),
            "--parallel",
            "1",
            "--gpu",
            "off",
            "--yes",
        ],
        folder,
    )
    state = loaded(inventory())
    if len(state) != 1 or state[0]["key"] != model:
        raise RuntimeError("unexpected_loaded_model")
    actual = state[0]["config"]
    if actual["context_length"] != context or actual["parallel"] != 1:
        raise RuntimeError("load_configuration_mismatch")
    return {
        "estimated_bytes": required,
        "memory_before_load": observed,
        "load_seconds": time.monotonic() - started,
        "actual": state[0],
    }


def restore(executable, original, allowed, folder):
    current = loaded(inventory())
    if any(row["key"] not in allowed for row in current):
        raise RuntimeError("unexpected_loaded_model")
    for row in current:
        run_control(executable, ["unload", row["id"]], folder)
    for row in original:
        config = row["config"]
        run_control(
            executable,
            [
                "load",
                row["key"],
                "--identifier",
                row["id"],
                "--context-length",
                str(config["context_length"]),
                "--parallel",
                str(config["parallel"]),
                "--gpu",
                "off",
                "--yes",
            ],
            folder,
        )
    actual = loaded(inventory())
    if actual != original:
        raise RuntimeError("restoration_mismatch")
    return actual


def image_messages(case, root, reverse=False):
    image_rows = case.get("images", [case.get("image")])
    if reverse:
        image_rows = list(reversed(image_rows))
    content = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "task": case["task"],
                    "scene": case["scene"],
                    "criteria": [
                        {"id": item["id"], "statement": item["statement"]}
                        for item in case["criteria"]
                    ],
                    "instructions": (
                        "Inspect visible image evidence. "
                        "Use unclear when evidence is insufficient. "
                        "Do not assume the image matches the scene. "
                        "For pairs, reject both when neither is suitable."
                    ),
                },
                ensure_ascii=False,
            ),
        }
    ]
    hashes = case.get("sha256")
    for name in image_rows:
        path = (root / name).resolve()
        if not path.is_relative_to((root / ".artifacts/data").resolve()):
            raise ValueError("Images must be retained workspace artifacts")
        content_bytes = path.read_bytes()
        expected = hashes if isinstance(hashes, str) else dict(hashes or {}).get(name)
        if not expected or hashlib.sha256(content_bytes).hexdigest() != expected:
            raise ValueError("Image content no longer matches the fixed manifest")
        encoded = base64.b64encode(content_bytes).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}}
        )
    return [{"role": "user", "content": content}]


def assessment(model, messages, schema, ids=()):
    from langsmith import tracing_context

    with tracing_context(enabled=False):
        response = model.bind(
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "image_review",
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            }
        ).invoke(messages, config={"callbacks": []})
    content = response if isinstance(response, str) else response.content
    metadata = {} if isinstance(response, str) else response.response_metadata
    row = {
        "response": content,
        "metadata": metadata,
        "usage": None if isinstance(response, str) else response.usage_metadata,
    }
    if metadata.get("finish_reason") not in {None, "stop"}:
        return {**row, "failure": "output_truncated"}
    try:
        parsed = schema.model_validate_json(content)
        if isinstance(parsed, ImageAssessment) and (
            len(parsed.checks) != len(ids) or {item.id for item in parsed.checks} != set(ids)
        ):
            raise ValueError("Mismatched criteria")
    except ValueError:
        return {**row, "failure": "invalid_assessment"}
    return {**row, "parsed": parsed.model_dump()}


def blind_content(evidence):
    """Remove provider identity while retaining successful and rejected visible content."""
    responses = []
    for value in evidence.get("objects", {}).values():
        if isinstance(value, dict) and isinstance(value.get("response"), dict):
            content = value["response"].get("content")
            if isinstance(content, dict) and "object_ref" in content:
                content = evidence["objects"][content["object_ref"]]
            responses.append(content)
    return {
        key: evidence.get(key)
        for key in ("source", "story", "characters", "props", "supplements", "scenes")
    } | {"responses": responses}


def runtime_observation():
    """Observe Windows paging and actual engine flags without changing runtime state."""
    if os.name != "nt":
        return {"platform": os.name}
    command = r"""
    [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new()
    $m=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    $p=@(Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" | ForEach-Object {
        $gpu=[regex]::Match($_.CommandLine,'--n-gpu-layers\s+(\d+)')
        $threads=[regex]::Match($_.CommandLine,'--threads\s+(\d+)')
        [pscustomobject]@{pid=$_.ProcessId;engine=$_.ExecutablePath;
            gpu_layers=if($gpu.Success){[int]$gpu.Groups[1].Value}else{$null};
            threads=if($threads.Success){[int]$threads.Groups[1].Value}else{$null}}
    })
    [pscustomobject]@{page_reads_per_second=$m.PageReadsPerSec;
        pages_input_per_second=$m.PagesInputPerSec;processes=$p}|ConvertTo-Json -Depth 4
    """
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("runtime_observation_failed")
    return json.loads(result.stdout)
