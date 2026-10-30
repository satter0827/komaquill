"""Check symmetric prompt sources before any generation side effect."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from komaquill.cli.image_inputs import image_input
from komaquill.domain.errors import KomaquillError


@pytest.mark.parametrize("bom", [False, True])
def test_positive_file_preserves_text_and_source_hash(tmp_path, bom):
    positive = tmp_path / "positive.txt"
    codec = "utf-8-sig" if bom else "utf-8"
    positive.write_text("雨の日の猫\nblue umbrella", encoding=codec, newline="")
    value, sources = image_input(None, positive)
    assert value.prompt == "雨の日の猫\nblue umbrella"
    assert [source.role for source in sources] == ["positive"]
    for file, source in zip((positive,), sources, strict=True):
        assert source.name == file.name and source.sha256 == sha256(file.read_bytes()).hexdigest()
        assert source.content.encode("utf-8") == file.read_bytes()


def test_json_accepts_current_prompt_format_and_subjects(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 4,
                "prompt": "a street",
                "subjects": ["woman", "kitten"],
            }
        ),
        encoding="utf-8-sig",
    )
    value, sources = image_input(None, input_file=path)
    assert value.prompt == "a street"
    assert value.subjects == ("woman", "kitten") and sources[0].role == "input"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": "a", "prompt_file": Path("positive.txt")},
        {"prompt": "a", "input_file": Path("input.json")},
        {"prompt": None, "prompt_file": Path("positive.txt"), "input_file": Path("input.json")},
    ],
)
def test_conflicting_sources_are_rejected_without_opening_any_file(kwargs):
    with pytest.raises(KomaquillError) as error:
        image_input(**kwargs)
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize(
    "content",
    [
        b"not-json",
        b'{"prompt":""}',
        b'{"prompt":"ok","negative":4}',
        b'{"prompt":"ok","subjects":["valid",""]}',
        b'{"prompt":"ok","extra":"ignored?"}',
        b'{"format_version":1,"prompt":"ok"}',
    ],
)
def test_invalid_input_files_are_safe_errors(tmp_path, content):
    file = tmp_path / "bad.json"
    file.write_bytes(content)
    with pytest.raises(KomaquillError) as error:
        image_input(None, input_file=file)
    assert error.value.display_key == "inputs.invalid_json"
    assert "ignored" not in str(error.value)


@pytest.mark.parametrize("content", [b"", b"   ", b"\xff"])
def test_empty_or_non_utf8_positive_is_rejected(tmp_path, content):
    file = tmp_path / "positive.txt"
    file.write_bytes(content)
    with pytest.raises(KomaquillError):
        image_input(None, prompt_file=file)


def test_missing_file_is_a_safe_error(tmp_path):
    with pytest.raises(KomaquillError):
        image_input(None, prompt_file=tmp_path / "missing.txt")


@pytest.mark.parametrize("negative", [None, "", "extra hands"])
def test_removed_json_input_is_rejected_even_when_empty(tmp_path, negative):
    file = tmp_path / "input.json"
    file.write_text(json.dumps({"prompt": "a street", "profile": negative}))
    with pytest.raises(KomaquillError) as error:
        image_input(None, input_file=file)
    assert error.value.display_key == "inputs.invalid_json"
