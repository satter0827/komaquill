"""Exercise provider evidence and non-allocating transport through local doubles."""

import hashlib
import json
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from komaquill.application.lifecycle import ShutdownActions, shutdown
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import RuntimeObservation
from komaquill.infrastructure import colab_bridge as bridge
from komaquill.infrastructure.colab import Colab, Receipt
from komaquill.infrastructure.storage import atomic_json

SUBJECT = hashlib.sha256(b"account-subject").hexdigest()


@pytest.fixture
def provider(project, monkeypatch):
    assignment = SimpleNamespace(
        endpoint="expected",
        runtime_proxy_info=SimpleNamespace(
            token="private-token", url="https://runtime.example", expires_at=lambda: None
        ),
    )
    state = [assignment]
    calls = []
    client = SimpleNamespace(
        session=SimpleNamespace(
            get=lambda *args, **kwargs: SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: {"sub": "account-subject"}
            )
        ),
        list_assignments=lambda: list(state),
        unassign=lambda endpoint: calls.append(endpoint) or state.clear(),
    )
    receipt = {
        "name": "owned",
        "runtime_id": str(uuid4()),
        "endpoint_id": "expected",
        "account_sha256": SUBJECT,
        "settings": project.settings().model_dump(mode="json"),
    }
    monkeypatch.setattr(bridge, "provider", lambda: client)
    return client, state, calls, receipt


def test_absent_cli_cache_is_independent_of_provider_liveness(provider, project, monkeypatch):
    client, state, calls, data = provider
    control = Colab(project)
    control.config = project.state / "missing.json"
    receipt = Receipt.model_validate(data)
    atomic_json(control.receipt_file, data)
    monkeypatch.setattr(
        control,
        "provider_call",
        lambda action, *args, **kwargs: bridge.execute(action, data, *args),
    )
    assert control.entry(receipt) is None
    assert control.refresh(receipt)
    result = control.stop()
    assert result.stopped and calls == ["expected"]
    assert control.receipt_file.exists()
    assert not control.refresh(receipt)
    assert not control.config.exists()


@pytest.mark.parametrize("case", ["account", "endpoint", "unavailable"])
def test_uncertain_observation_never_stops_or_releases(provider, project, monkeypatch, case):
    client, state, calls, data = provider
    if case == "account":
        data["account_sha256"] = "other"
    elif case == "endpoint":
        data["endpoint_id"] = None
    else:
        client.list_assignments = lambda: (_ for _ in ()).throw(OSError("private-response"))
    control = Colab(project)
    control.config = project.state / "missing.json"
    atomic_json(control.receipt_file, data)
    monkeypatch.setattr(control, "provider_call", lambda action: bridge.execute(action, data))
    with pytest.raises((KomaquillError, OSError)):
        control.stop()
    assert control.receipt_file.exists() and not calls


def test_shutdown_rejects_missing_or_unknown_confirmation():
    for observation in [None, RuntimeObservation(), RuntimeObservation(state="stopped")]:
        calls = []
        result, released = shutdown(
            uuid4(),
            True,
            ShutdownActions(
                collect=lambda: None,
                logs=lambda: (),
                stop=lambda observation=observation: observation,
                disconnect=lambda calls=calls: calls.append("disconnect"),
                finalize=lambda calls=calls: calls.append("lost"),
                persist=lambda _: None,
                release=lambda calls=calls: calls.append("release"),
                display=lambda _: None,
                forced=lambda: False,
            ),
        )
        assert result.gpu == "unknown" and not released
        assert calls == ["disconnect"]


def test_proxy_and_download_use_fresh_assignment_without_cli_selection(provider, monkeypatch):
    client, state, calls, data = provider
    modules = bridge.importlib.import_module

    def imported(name):
        if name == "colab_cli.state":
            return SimpleNamespace(SessionState=lambda **values: SimpleNamespace(**values))
        if name == "colab_cli.commands.ssh":
            return SimpleNamespace(
                _run_proxy_bridge=lambda s, **kw: calls.append((s.endpoint, kw)) or 0
            )
        if name == "colab_cli.contents":
            return SimpleNamespace(
                ContentsClient=lambda s: SimpleNamespace(download=lambda a, b: calls.append((a, b)))
            )
        return modules(name)

    monkeypatch.setattr(bridge.importlib, "import_module", imported)
    assert bridge.execute("proxy", data, "key").state == "running"
    assert calls == [("expected", {"identity": "key", "rm": False})]
    bridge.execute("download", data, "remote.log", "local.log")
    assert calls[-1] == ("remote.log", "local.log")
    state.clear()
    with pytest.raises(KomaquillError):
        bridge.execute("proxy", data, "key")


def test_bridge_cli_hides_provider_failures(provider, project, monkeypatch, capsys):
    _, _, _, data = provider
    path = project.root / "receipt.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(sys, "argv", ["bridge", "observe", "--receipt", str(path)])
    bridge.main()
    output = capsys.readouterr().out
    assert json.loads(output)["state"] == "running" and "private-token" not in output
    monkeypatch.setattr(
        bridge, "execute", lambda *args: (_ for _ in ()).throw(RuntimeError("private-token"))
    )
    with pytest.raises(SystemExit) as error:
        bridge.main()
    assert error.value.code == 1 and "private-token" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "case", ["identity", "observe", "terminate", "timeout", "unknown", "invalid", "proxy_error"]
)
def test_provider_actions_report_evidence_or_safe_failure(provider, monkeypatch, case):
    client, state, calls, data = provider
    if case == "timeout":
        client.unassign = lambda _: None
        ticks = iter([0, 1000])
        monkeypatch.setattr(bridge.time, "monotonic", lambda: next(ticks))
    if case == "unknown":
        data["account_sha256"] = "other"
    if case == "proxy_error":

        def imported(name):
            if name.endswith("state"):
                return SimpleNamespace(SessionState=lambda **kw: SimpleNamespace(**kw))
            return SimpleNamespace(_run_proxy_bridge=lambda *a, **kw: 1)

        monkeypatch.setattr(bridge.importlib, "import_module", imported)
    if case in {"timeout", "unknown", "invalid", "proxy_error"}:
        with pytest.raises(KomaquillError):
            bridge.execute(
                {"timeout": "terminate", "unknown": "download", "proxy_error": "proxy"}.get(
                    case, case
                ),
                data,
                "key",
            )
    else:
        result = bridge.execute(case, data)
        assert result.account_sha256 == SUBJECT
        if case == "terminate":
            assert result.stopped
            assert bridge.execute(case, data).stopped


def test_invalid_subject_is_not_accepted(provider):
    client, _, _, _ = provider
    client.session.get = lambda *a, **kw: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"sub": ""}
    )
    with pytest.raises(KomaquillError):
        bridge.account(client, 1)


def test_provider_uses_saved_credentials_without_interactive_login(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.Path, "home", lambda: tmp_path)
    with pytest.raises(KomaquillError):
        bridge.provider()
    token = tmp_path / ".config/colab-cli/token.json"
    token.parent.mkdir(parents=True)
    token.write_text("{}")

    def imported(name):
        if name == "google.oauth2.credentials":
            return SimpleNamespace(
                Credentials=SimpleNamespace(from_authorized_user_file=lambda p: p)
            )
        if name == "google.auth.transport.requests":
            return SimpleNamespace(AuthorizedSession=lambda credentials: credentials)
        return SimpleNamespace(
            Prod=lambda: "production", Client=lambda env, session: (env, session)
        )

    monkeypatch.setattr(bridge.importlib, "import_module", imported)
    assert bridge.provider() == ("production", str(token))


def test_proxy_entrypoint_emits_no_json_into_ssh_stream(provider, project, monkeypatch, capsys):
    _, _, _, data = provider
    path = project.root / "receipt.json"
    path.write_text(json.dumps(data))
    calls = []
    monkeypatch.setattr(sys, "argv", ["bridge", "proxy", "--receipt", str(path), "key"])
    monkeypatch.setattr(bridge, "execute", lambda *args: calls.append(args) or RuntimeObservation())
    bridge.main()
    assert not capsys.readouterr().out
    assert calls[0][0] == "proxy" and calls[0][-1] == "key"


@pytest.mark.parametrize("case", ["success", "invalid", "timeout"])
def test_bounded_provider_child_validates_output_without_revealing_stderr(
    project, monkeypatch, case
):
    import subprocess

    from komaquill.infrastructure import colab as module

    control = Colab(project)
    monkeypatch.setattr(control, "provider_command", lambda *args: ["provider", *args])

    def run(*args, **kwargs):
        assert kwargs["timeout"] == control.settings.control_timeout
        if case == "timeout":
            raise subprocess.TimeoutExpired("provider", 1)
        return SimpleNamespace(
            stdout=b"private-response"
            if case == "invalid"
            else RuntimeObservation().model_dump_json()
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    if case == "success":
        assert control.provider_call("observe").state == "unknown"
    else:
        with pytest.raises(KomaquillError) as error:
            control.provider_call("observe")
        assert "private-response" not in str(error.value)
