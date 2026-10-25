"""Check symmetric prompt sources before any generation side effect."""

import json
from hashlib import sha256
from pathlib import Path

import pytest

from komaquill.domain.errors import KomaquillError
from komaquill.interfaces.image_inputs import image_input, negative_input


@pytest.mark.parametrize("bom", [False, True])
def test_positive_and_negative_files_preserve_text_and_source_hashes(tmp_path, bom):
    positive = tmp_path / "positive.txt"
    negative = tmp_path / "negative.txt"
    codec = "utf-8-sig" if bom else "utf-8"
    positive.write_text("雨の日の猫\nblue umbrella", encoding=codec, newline="")
    negative.write_text("extra limbs\ntext", encoding=codec, newline="")
    value, sources = image_input(None, None, positive, negative)
    assert value.prompt == "雨の日の猫\nblue umbrella"
    assert value.negative == "extra limbs\ntext"
    assert [source.role for source in sources] == ["positive", "negative"]
    for file, source in zip((positive, negative), sources, strict=True):
        assert source.name == file.name and source.sha256 == sha256(file.read_bytes()).hexdigest()
        assert source.content.encode("utf-8") == file.read_bytes()


@pytest.mark.parametrize("negative", [None, "", "extra hands"])
def test_json_accepts_matching_prompt_formats_and_subjects(tmp_path, negative):
    path = tmp_path / "input.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "prompt": "a street",
                "negative": negative,
                "subjects": ["woman", "kitten"],
            }
        ),
        encoding="utf-8-sig",
    )
    value, sources = image_input(None, None, input_file=path)
    assert value.prompt == "a street" and value.negative == negative
    assert value.subjects == ("woman", "kitten") and sources[0].role == "input"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": "a", "prompt_file": Path("positive.txt")},
        {"prompt": "a", "negative": "b", "negative_file": Path("negative.txt")},
        {"prompt": "a", "input_file": Path("input.json")},
        {"prompt": None, "negative": "", "input_file": Path("input.json")},
        {"prompt": None, "prompt_file": Path("positive.txt"), "input_file": Path("input.json")},
        {"prompt": None, "negative_file": Path("negative.txt"), "input_file": Path("input.json")},
    ],
)
def test_conflicting_sources_are_rejected_without_opening_any_file(kwargs):
    with pytest.raises(KomaquillError) as error:
        image_input(negative=None, **kwargs) if "negative" not in kwargs else image_input(**kwargs)
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize(
    "content",
    [
        b"not-json",
        b'{"prompt":""}',
        b'{"prompt":"ok","negative":4}',
        b'{"prompt":"ok","subjects":["1","2","3"]}',
        b'{"prompt":"ok","extra":"ignored?"}',
        b'{"format_version":3,"prompt":"ok"}',
    ],
)
def test_invalid_input_files_are_safe_errors(tmp_path, content):
    file = tmp_path / "bad.json"
    file.write_bytes(content)
    with pytest.raises(KomaquillError) as error:
        image_input(None, None, input_file=file)
    assert error.value.display_key == "inputs.invalid_json"
    assert "ignored" not in str(error.value)


@pytest.mark.parametrize("content", [b"", b"   ", b"\xff"])
def test_empty_or_non_utf8_positive_is_rejected(tmp_path, content):
    file = tmp_path / "positive.txt"
    file.write_bytes(content)
    with pytest.raises(KomaquillError):
        image_input(None, None, prompt_file=file)


def test_missing_file_and_explicit_empty_negative(tmp_path):
    with pytest.raises(KomaquillError):
        negative_input(None, tmp_path / "missing.txt")
    file = tmp_path / "empty.txt"
    file.write_bytes(b"")
    value, sources = image_input("a cabin", None, negative_file=file)
    assert value.negative == "" and sources[0].role == "negative"
    assert negative_input(None, None) == (None, ())
