"""Exercise live-run orchestration without permitting inference or allocation."""

import json
import sys
from types import SimpleNamespace

import pytest
from komaquill.api import Client, KomaquillError

from scripts.acceptance import budget, run


@pytest.mark.parametrize("failure", [None, "storage_failed", "interrupt"])
def test_attempt_retains_failures_and_a_shared_durable_allowance(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(run, "ROOT", tmp_path)
    output = tmp_path / ".artifacts/live/attempt-1"
    monkeypatch.setattr(sys, "argv", ["live", "--allow-real-llm", "--output", str(output)])

    def collect(api, directory, report):
        assert api.llm_usage()["reserved_calls"] == 0
        if failure == "interrupt":
            raise KeyboardInterrupt()
        if failure:
            raise KomaquillError(code=failure)
        report["cases"].append({"case": "orchestration", "state": "complete"})
        return api, []

    monkeypatch.setattr(run, "conversations", collect)
    monkeypatch.setattr(run, "observe", lambda *args: None)
    assert run.main() == (1 if failure else 0)
    report = json.loads((output / "run.json").read_bytes())
    assert report["status"] == (
        "interrupted" if failure == "interrupt" else "failed" if failure else "passed"
    )
    assert report["inputs_unchanged"]
    assert report["budget"]["reserved_calls"] == 0
    api = Client(output.parent / "workspace")
    run.attach(api, output.parent / "attempt-2")
    assert api.llm_usage() == report["budget"]
    api.close()


def test_live_gate_requires_permission_and_private_output(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "ROOT", tmp_path)
    for args in (
        ["--output", str(tmp_path / ".artifacts/live")],
        ["--allow-real-llm", "--output", str(tmp_path / "public")],
    ):
        monkeypatch.setattr(sys, "argv", ["live", *args])
        with pytest.raises(SystemExit):
            run.main()
    assert not (tmp_path / "public").exists()


def test_ui_acceptance_uses_the_isolated_browser_and_its_call_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "ROOT", tmp_path)
    output = tmp_path / ".artifacts/ui/attempt-1"
    monkeypatch.setattr(sys, "argv", ["live", "--ui", "--allow-real-llm", "--output", str(output)])

    def browse(api, directory, report, scenario, *, script, timeout):
        assert directory == output
        assert api.llm_usage()["max_calls"] == run.LIMITS["ui"]["max_calls"]
        assert scenario == {"format_version": 1, "real_llm": True}
        assert script == "scripts/acceptance/ui.mjs"
        assert timeout == run.LIMITS["ui"]["max_seconds"]
        assert report["real_gpu"] is False

    monkeypatch.setattr(run, "browse", browse)
    assert run.main() == 0
    report = json.loads((output / "run.json").read_bytes())
    assert report["new_llm_calls"] == 0
    assert report["status"] == "passed"


def test_unowned_runtime_is_never_stopped_or_allocated(tmp_path):
    calls = []
    api = SimpleNamespace(
        runtime=SimpleNamespace(
            status=lambda: SimpleNamespace(gpu="active", collector_running=False),
            usage=lambda: SimpleNamespace(assignments_count=1),
            start=lambda **kw: calls.append("start"),
            stop=lambda: calls.append("stop"),
        )
    )
    with pytest.raises(KomaquillError, match="operation_busy"):
        run.gpu(api, tmp_path, {"cases": []}, [])
    assert calls == []


def test_saved_observation_uses_the_recorded_turn_count(tmp_path, monkeypatch):
    from uuid import uuid4

    identity = uuid4()
    captured = []
    api = SimpleNamespace(
        conversations=SimpleNamespace(overview=lambda _: SimpleNamespace(turn_count=20)),
        images=SimpleNamespace(list=lambda: []),
    )
    monkeypatch.setattr(
        run, "browse", lambda api, output, report, scenario: captured.append(scenario)
    )
    run.observe(api, tmp_path, {}, [identity])
    assert captured[0]["format_version"] == 1
    assert captured[0]["conversation_turn_counts"] == {str(identity): 20}


@pytest.mark.parametrize(
    "stage,shutdown,saved_only",
    [
        ("start", "stopped", False),
        ("drawing", "stopped", False),
        (None, "stopped", False),
        (None, "unknown", True),
        (None, "stopped", True),
    ],
)
@pytest.mark.parametrize("conversation_only", [False, True])
def test_owned_gpu_reservations_and_cleanup_survive_every_failure(
    tmp_path, monkeypatch, stage, shutdown, saved_only, conversation_only
):
    from datetime import UTC, datetime
    from uuid import uuid4

    from komaquill.api import AccountUsage, SessionStatus

    Client.initialize(tmp_path).close()
    calls = []
    usage = AccountUsage(
        remaining_compute_units=100,
        consumption_rate_hourly=2,
        assignments_count=0,
        observed_at=datetime.now(UTC),
        account_sha256="a" * 64,
    )

    def start(**options):
        calls.append("start")
        if stage == "start":
            raise KomaquillError(code="connection_failed")
        return SessionStatus(gpu="active")

    def execute(identity):
        calls.append("drawing")
        if stage == "drawing":
            raise KomaquillError(code="generation_failed")
        return SimpleNamespace(image_id=uuid4())

    preset = SimpleNamespace(id=uuid4(), revision=0)
    api = SimpleNamespace(
        workspace=tmp_path,
        close=lambda: None,
        runtime=SimpleNamespace(
            status=lambda: SessionStatus(gpu="absent"),
            usage=lambda: usage,
            start=start,
            stop=lambda: calls.append("stop") or SessionStatus(gpu=shutdown),
            views=lambda: [],
        ),
        characters=SimpleNamespace(create=lambda *args: preset),
        stories=SimpleNamespace(create=lambda *args: preset),
        conversations=SimpleNamespace(overview=lambda *args: preset),
        library=SimpleNamespace(accept=lambda *args: None, execute=execute),
        images=SimpleNamespace(submit=lambda *args, **kw: SimpleNamespace(run_id=uuid4())),
    )
    monkeypatch.setattr(run, "Client", lambda *args: api)
    monkeypatch.setattr(run, "save_image", lambda *args: calls.append("saved"))
    monkeypatch.setattr(run, "observe", lambda *args: calls.append("observed"))
    report = {"cases": []}
    if stage or shutdown == "unknown":
        with pytest.raises(KomaquillError):
            run.gpu(
                api,
                tmp_path,
                report,
                [uuid4()],
                saved_only=saved_only,
                conversation_only=conversation_only,
            )
    else:
        run.gpu(
            api,
            tmp_path,
            report,
            [uuid4()],
            saved_only=saved_only,
            conversation_only=conversation_only,
        )
    assert calls.count("start") == calls.count("stop") == 1
    ledger = budget.read(tmp_path)
    assert ledger.allocations[-1].ownership_released == (shutdown == "stopped")
    if saved_only:
        assert not ledger.reservations and "drawing" not in calls
    elif stage is None:
        assert len(ledger.reservations) == (2 if conversation_only else 4)
    if stage:
        assert report["gpu"]["failure"]["code"] == (
            "connection_failed" if stage == "start" else "generation_failed"
        )
    assert json.loads((tmp_path / "run.json").read_bytes())["gpu"]


@pytest.mark.parametrize("failure", ["budget", "close"])
def test_finalization_failure_is_retained_without_private_exception_text(
    tmp_path, monkeypatch, failure
):
    root = tmp_path / ".artifacts/live"
    Client.initialize(root / "workspace").close()
    calls = []

    def usage():
        calls.append("usage")
        if failure == "budget" and len(calls) > 1:
            raise OSError("private-budget-detail")
        return {"reserved_calls": 0}

    def close():
        if failure == "close":
            raise OSError("private-close-detail")

    api = SimpleNamespace(llm_usage=usage, close=close, limit_llm_calls=lambda *a, **k: None)
    monkeypatch.setattr(run, "Client", lambda *args: api)
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "conversations", lambda api, *args: (api, []))
    monkeypatch.setattr(run, "observe", lambda *args: None)
    output = root / "failed"
    monkeypatch.setattr(sys, "argv", ["live", "--allow-real-llm", "--output", str(output)])
    assert run.main() == 1
    raw = (output / "run.json").read_text()
    assert "private-" not in raw
    assert json.loads(raw)["status"] == "failed"


def test_saved_result_resume_uses_no_new_inference(tmp_path, monkeypatch):
    from uuid import uuid4

    root = tmp_path / ".artifacts/live"
    previous = root / "attempt-1/run.json"
    previous.parent.mkdir(parents=True)
    previous.write_text(
        json.dumps(
            {"cases": [{"operation_id": str(uuid4()), "state": "complete"} for _ in range(2)]}
        )
    )
    (root / "workspace").mkdir()
    api = SimpleNamespace(
        workspace=root / "workspace",
        close=lambda: None,
        limit_llm_calls=lambda *args, **kw: None,
        llm_usage=lambda: {"reserved_calls": 3},
        operations=SimpleNamespace(get=lambda value: SimpleNamespace(conversation_id=value)),
    )
    monkeypatch.setattr(run, "Client", lambda *args: api)
    monkeypatch.setattr(run, "ROOT", tmp_path)
    observed = []
    monkeypatch.setattr(run, "observe", lambda *args: observed.append(args[-1]))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live",
            "--allow-real-llm",
            "--resume",
            str(previous),
            "--output",
            str(root / "attempt-2"),
        ],
    )
    assert run.main() == 0
    assert len(observed[0]) == 2
    assert json.loads((root / "attempt-2/run.json").read_bytes())["budget"]["reserved_calls"] == 3
    assert json.loads((root / "attempt-2/run.json").read_bytes())["new_llm_calls"] == 0
    # A resumed attempt is itself a valid source without containing new operations.
    api.operations.get = lambda value: pytest.fail("Read the stored conversation identities.")
    sys.argv[sys.argv.index(str(previous))] = str(root / "attempt-2/run.json")
    sys.argv[-1] = str(root / "attempt-3")
    assert run.main() == 0
    assert observed[1] == observed[0]
