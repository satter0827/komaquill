"""Exercise GPU preparation boundaries without allocating a GPU or downloading data."""

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from komaquill.domain.errors import KomaquillError
from komaquill.gpu import bootstrap as bootstrap
from komaquill.gpu import worker as worker


@pytest.mark.parametrize("case", ["download", "cached", "checksum", "missing"])
def test_bootstrap_verifies_tools_and_starts_isolated_worker(tmp_path, monkeypatch, case):
    script = tmp_path / "komaquill/gpu/bootstrap.py"
    config = tmp_path / "komaquill/resources/runtime.toml"
    config.parent.mkdir(parents=True)
    config.write_text('uv_version="test-version"\npython_version="3.12.15"')
    monkeypatch.setattr(bootstrap, "__file__", str(script))
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as archive:
        member = tarfile.TarInfo("wrong" if case == "missing" else "uv-x86_64-unknown-linux-gnu/uv")
        payload = b"verified uv executable"
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    data = content.getvalue()

    def download(url, **kwargs):
        if url.endswith(".sha256"):
            return io.BytesIO(
                ("0" * 64 if case == "checksum" else hashlib.sha256(data).hexdigest()).encode()
            )
        return io.BytesIO(data)

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", download)
    commands = []
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda args, **kwargs: commands.append((args, kwargs))
    )
    uv = tmp_path / ".artifacts/runtime/tools/uv"
    if case == "cached":
        uv.parent.mkdir(parents=True)
        uv.write_bytes(b"cached")
    if case in {"checksum", "missing"}:
        with pytest.raises(RuntimeError):
            bootstrap.main()
        assert not commands
    else:
        bootstrap.main()
        assert uv.is_file() and len(commands) == 3
        assert commands[0][0][1:3] == ["venv", "--python"]
        assert "--require-hashes" in commands[1][0]
        assert commands[2][0][2:4] == ["-m", "komaquill.gpu.worker"]
        assert all(
            command[1]["env"]["TMPDIR"] == str(tmp_path / ".artifacts/tmp") for command in commands
        )
        assert commands[0][1]["env"]["UV_PROJECT_ENVIRONMENT"] == str(
            tmp_path / ".artifacts/runtime/venv"
        )


@pytest.mark.parametrize("exists,mismatch", [(False, False), (True, False), (True, True)])
def test_server_preparation_pins_source_and_registers_both_views(
    project, monkeypatch, exists, mismatch
):
    comfy = project.runtime / "ComfyUI"
    if exists:
        comfy.mkdir(parents=True)
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        if "clone" in args:
            comfy.mkdir(parents=True)

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(
        worker.subprocess,
        "check_output",
        lambda *args, **kwargs: "wrong" if mismatch else "revision",
    )
    model = project.artifacts / "models/example.safetensors"
    monkeypatch.setattr(worker, "fetch_model", lambda *args: model)
    user = project.runtime / "user"
    if mismatch:
        with pytest.raises(KomaquillError):
            worker.prepare_server(project, {"comfy_revision": "revision"}, user)
    else:
        assert worker.prepare_server(project, {"comfy_revision": "revision"}, user) == comfy
        templates = comfy / "custom_nodes/komaquill/example_workflows"
        assert json.loads((templates / "pony_app.json").read_bytes())["extra"]["linearMode"] is True
        assert (
            json.loads((templates / "pony_graph.json").read_bytes())["extra"]["linearMode"] is False
        )
        assert json.loads((project.runtime / "model-paths.json").read_bytes())["pony"][
            "base_path"
        ] == str(model.parent)
        assert user.is_dir()
        assert bool(commands) == (not exists)


class Server:
    def __init__(self, *, early_exit=False, exit_code=0):
        self.early_exit = early_exit
        self.exit_code = exit_code
        self.finished = False
        self.terminated = False

    def poll(self):
        return self.exit_code if self.finished or self.early_exit else None

    def wait(self, **kwargs):
        self.finished = True
        return self.exit_code

    def terminate(self):
        self.terminated = True


@pytest.mark.parametrize(
    "case", ["success", "cpu", "early_exit", "timeout", "server_error", "retry"]
)
def test_worker_health_environment_and_cleanup(project, monkeypatch, case):
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: case != "cpu")),
    )
    monkeypatch.setenv("KOMAQUILL_RUNTIME_ID", "test-runtime")
    monkeypatch.setenv("KOMAQUILL_SOURCE_REVISION", "test-revision")
    monkeypatch.setattr(worker, "prepare_server", lambda *args: project.root / "comfy")
    server = Server(
        early_exit=case == "early_exit",
        exit_code=1 if case in {"early_exit", "server_error"} else 0,
    )
    commands = []
    monkeypatch.setattr(
        worker.subprocess, "Popen", lambda args, **kwargs: commands.append(args) or server
    )
    monkeypatch.setattr(
        worker.subprocess, "run", lambda args, **kwargs: kwargs["stdout"].write("torch==test\n")
    )
    calls = []

    def health(*args, **kwargs):
        calls.append(args)
        if case == "retry" and len(calls) == 1:
            raise httpx.ConnectError("temporary")
        return SimpleNamespace(is_success=True)

    monkeypatch.setattr(worker.httpx, "get", health)
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    if case == "timeout":
        ticks = iter([0, 10000])
        monkeypatch.setattr(worker.time, "monotonic", lambda: next(ticks))
    if case in {"cpu", "early_exit", "timeout", "server_error"}:
        with pytest.raises(KomaquillError):
            worker.main(project.root)
        if case == "timeout":
            assert server.terminated
        if case == "cpu":
            assert not commands and not project.runtime.exists()
    else:
        worker.main(project.root)
        metadata = json.loads(
            (project.runtime / "comfy-user/default/komaquill/session.json").read_bytes()
        )
        assert metadata["runtime_id"] == "test-runtime"
        assert (project.runtime / "environment.txt").read_text() == "torch==test\n"
        assert "127.0.0.1" in commands[0] and not server.terminated
        assert len(calls) == (2 if case == "retry" else 1)


def test_worker_entrypoint_hides_traceback_for_gpu_unavailable(project, monkeypatch, capsys):
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)),
    )
    monkeypatch.setattr(sys, "argv", ["worker.py", str(project.root)])
    namespace = {"__name__": "__main__", "__file__": worker.__file__}
    with pytest.raises(SystemExit) as error:
        exec(
            compile(Path(worker.__file__).read_text(encoding="utf-8"), worker.__file__, "exec"),
            namespace,
        )
    assert error.value.code == 1
    assert "GPU" in capsys.readouterr().err
