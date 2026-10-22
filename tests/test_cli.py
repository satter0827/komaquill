"""VS Codeに依存しない操作と画面利用中の自動保存を検証する。"""

from contextlib import contextmanager

from typer.testing import CliRunner

from komaquill import cli


def test_missing_connection_has_actionable_error(project):
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "collect"])
    assert result.exit_code == 1
    assert "start" in result.output
    assert "Traceback" not in result.output


def test_invalid_view_does_not_open_a_browser():
    result = CliRunner().invoke(cli.app, ["open", "--view", "unknown"])
    assert result.exit_code == 2


def test_open_returns_after_opening_the_selected_view(project, client, session, monkeypatch):
    @contextmanager
    def connection(_project):
        yield client, session

    opened = []
    monkeypatch.setattr(cli, "connected", connection)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    result = CliRunner().invoke(
        cli.app, ["--project", str(project.root), "open", "--view", "graph"]
    )
    assert result.exit_code == 0, result.output
    assert "template=sd15_graph" in opened[0]
    assert "source=komaquill" in opened[0]
    assert "start" in result.output


def test_status_without_a_session_never_creates_gpu(project, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("確認でGPUを確保してはいけない")

    monkeypatch.setattr(cli.Colab, "acquire", forbidden)
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "status"])
    assert result.exit_code == 0, result.output
    assert "未回収: 0件" in result.output


def test_collect_reports_partial_save_and_returns_failure(
    project, client, session, stub, monkeypatch
):
    from komaquill.workflow import resource_json

    @contextmanager
    def connection(_project):
        yield client, session

    client.submit(resource_json("workflow-api.json"), {}, "broken-browser")
    client.submit(resource_json("workflow-api.json"), {}, "healthy-browser")
    record = next(iter(stub.history.values()))
    record["outputs"]["9"]["images"][0]["filename"] = "broken.png"
    stub.invalid_images.add("broken.png")
    monkeypatch.setattr(cli, "connected", connection)
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "collect"])
    assert result.exit_code == 1
    assert "保存 1件" in result.output and "再回収" in result.output
    assert "Traceback" not in result.output


def test_status_separates_lost_requests_from_pending(project, session):
    from uuid import uuid4

    from komaquill.service import mark_lost, new_run
    from komaquill.storage import atomic_json, save_run

    save_run(project, new_run(project, session, uuid4(), {}))
    mark_lost(project, session.runtime_id)
    atomic_json(project.session_file, session.model_dump(mode="json"))
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "status"])
    assert result.exit_code == 0
    assert "未回収: 0件" in result.output and "回収不能: 1件" in result.output
    assert "ComfyUI: 終了済み" in result.output


def test_status_does_not_wait_on_allocation_lock(project, monkeypatch):
    from uuid import uuid4

    from komaquill.colab import Colab, Receipt
    from komaquill.storage import atomic_json

    colab = Colab(project)
    receipt = Receipt(name="starting", runtime_id=uuid4(), source_revision="a" * 40)
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    monkeypatch.setattr(cli, "Colab", lambda project: colab)
    # 別インスタンスでロックし、同じスレッドの再入を避ける。
    from filelock import FileLock

    with FileLock(str(project.runtime / ".colab.lock")):
        result = CliRunner().invoke(cli.app, ["--project", str(project.root), "status"])
    assert result.exit_code == 0
    assert "Colab: 起動または終了の処理中" in result.output
    assert "ComfyUI: 準備中" in result.output


def test_status_does_not_call_uncertain_allocation_terminated(project, monkeypatch):
    from uuid import uuid4

    from komaquill.colab import Colab, Receipt
    from komaquill.storage import atomic_json

    colab = Colab(project)
    receipt = Receipt(name="uncertain", runtime_id=uuid4(), source_revision="a" * 40)
    atomic_json(colab.receipt_file, receipt.model_dump(mode="json"))
    monkeypatch.setattr(colab, "refresh", lambda receipt: False)
    monkeypatch.setattr(cli, "Colab", lambda project: colab)
    result = CliRunner().invoke(cli.app, ["--project", str(project.root), "status"])
    assert result.exit_code == 0
    assert "確保結果が未確認" in result.output
    assert "ComfyUI: 未起動" in result.output
    assert "終了済み" not in result.output
    assert colab.receipt_file.is_file()
