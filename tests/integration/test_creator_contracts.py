"""Verify migration preservation and image text boundaries without real inference."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from komaquill.adapters import database as storage
from komaquill.adapters import database_migration
from komaquill.adapters.conversation_llm import check_language
from komaquill.adapters.image_text import ImageTextPreparation
from komaquill.adapters.workspace import RuntimeWorkspace
from komaquill.api import Client, KomaquillError
from komaquill.conversations.models import ConversationInput, SendInput


def downgrade(api, revision="0004"):
    engine = api.conversations.repository.database.engine
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).with_name("migrations")))
    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


@pytest.mark.parametrize("case", ["database", "editor", "gone", "unknown", "monitor"])
def test_migration_stop_check_distinguishes_database_use_from_editor_tools(
    tmp_path, monkeypatch, case
):
    import psutil

    project = RuntimeWorkspace(tmp_path)
    monkeypatch.setattr(database_migration, "alive", lambda _: case == "monitor")

    def files():
        if case == "gone":
            raise psutil.NoSuchProcess(99999)
        if case == "unknown":
            raise psutil.AccessDenied(99999)
        return [
            SimpleNamespace(
                path=str(
                    project.data / ("komaquill.sqlite3" if case == "database" else "editor.log")
                )
            )
        ]

    process = SimpleNamespace(pid=99999, info={"name": "python.exe"}, open_files=files)
    monkeypatch.setattr(psutil, "process_iter", lambda _: [process])
    assert database_migration.database_in_use(project) == (
        case in {"database", "unknown", "monitor"}
    )


def test_upgrade_requires_an_explicit_backup_and_retains_existing_content(tmp_path):
    api = Client.initialize(tmp_path)
    saved = api.conversations.create(ConversationInput(title="Before migration"))
    api.conversations.send(saved.id, SendInput(operation_id=uuid4(), text="Hello"))
    before = api.conversations.get(saved.id)
    downgrade(api)
    with pytest.raises(KomaquillError, match="migration_required"):
        storage.Database(tmp_path / ".artifacts/data/komaquill.sqlite3")
    report = api.migrate()
    record = json.loads(report.read_bytes())
    assert record["state"] == "complete"
    assert (
        record["backup_sha256"]
        == hashlib.sha256(report.with_name("before.sqlite3").read_bytes()).hexdigest()
    )
    assert all(record["after"][key] == value for key, value in record["before"].items())
    assert Client(tmp_path).conversations.get(saved.id) == before
    assert Client(tmp_path).library.preferences().locale == "ja"


@pytest.mark.parametrize("case", ["busy", "missing", "backup_invalid", "after_invalid"])
def test_migration_failure_is_recorded_without_rewriting_existing_data(tmp_path, monkeypatch, case):
    api = Client.initialize(tmp_path)
    if case == "busy":
        monkeypatch.setattr(database_migration, "database_in_use", lambda *_, **__: True)
        expected = "operation_busy"
    elif case == "missing":
        expected = "request_not_found"
    else:
        api.conversations.create(ConversationInput(title="Kept"))
        original = database_migration.inventory
        calls = []

        def inventory(connection):
            calls.append(1)
            value = original(connection)
            if len(calls) == (2 if case == "backup_invalid" else 3):
                return {"changed": "invalid"}
            return value

        monkeypatch.setattr(database_migration, "inventory", inventory)
        expected = "storage_failed"
    with pytest.raises(KomaquillError, match=expected):
        api.migrate()


def test_image_translation_is_frozen_and_includes_original_sources(tmp_path, monkeypatch):
    prepare = ImageTextPreparation(tmp_path)
    calls = []
    monkeypatch.setattr(
        prepare.responder,
        "prepare_image",
        lambda text, *args: calls.append(text) or ("English scene" if text else ""),
    )
    identity = uuid4()
    result = prepare.prepare(identity, "雨の本屋", ("銀髪の女性",), "低画質", "lmstudio-4b", True)
    assert result.prompt == "English scene" and result.subjects == ("English scene",)
    assert [source.content for source in result.sources][::2] == [
        "雨の本屋",
        "銀髪の女性",
        "低画質",
    ]
    assert (
        prepare.prepare(identity, "雨の本屋", ("銀髪の女性",), "低画質", "lmstudio-4b", False)
        == result
    )
    assert len(calls) == 3
    with pytest.raises(KomaquillError, match="operation_conflict"):
        prepare.prepare(identity, "別の画像", (), None, "lmstudio-4b", True)
    unchanged = prepare.prepare(uuid4(), "a bookshop", (), None, "fake", False)
    assert unchanged.prompt == "a bookshop" and unchanged.sources == () and len(calls) == 3


def test_corrupt_translation_and_unapproved_inference_stop_before_images(tmp_path):
    api = Client.initialize(tmp_path)
    with pytest.raises(KomaquillError, match="real_llm_not_allowed"):
        api.images.submit("雨の本屋", llm_profile="lmstudio-4b")
    assert api.images.list() == []
    prepare = ImageTextPreparation(tmp_path)
    identity = uuid4()
    path = tmp_path / ".artifacts/data/image-preparations" / f"{identity}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("invalid")
    with pytest.raises(KomaquillError, match="storage_failed"):
        prepare.prepare(identity, "日本語", (), None, "lmstudio-4b", True)


@pytest.mark.parametrize(
    "text,locale,names,valid",
    [
        ("日本語の返事です。", "ja", (), True),
        ("An English response", "en", (), True),
        ("日本語の返事です。", "en", (), False),
        ("An English response", "ja", (), False),
        ("Hello 栞, welcome to the bookshop.", "en", ("栞",), True),
        ("Hello つむぎさん, welcome.", "en", ("つむぎ", "つむぎさん"), True),
    ],
)
def test_output_language_contract_preserves_proper_names(text, locale, names, valid):
    if valid:
        check_language(text, locale, names)
    else:
        with pytest.raises(ValueError):
            check_language(text, locale, names)
