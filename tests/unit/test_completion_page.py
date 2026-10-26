"""Check exported panel order, complete text and bounded page layout."""

import pytest
from PIL import Image

from tests.live.completion_page import render_page, wrap_text


def panels(tmp_path, text="The kitten is safe."):
    result = []
    for scene, color in enumerate(("red", "green", "blue", "yellow"), 1):
        path = tmp_path / f"{scene}.png"
        Image.new("RGB", (40, 40), color).save(path)
        result.append({"scene": scene, "key": f"saved/{scene}", "path": path, "text": text})
    return result


def test_png_and_pdf_have_right_to_left_order_and_complete_captions(tmp_path):
    result = render_page(tmp_path, "A rainy day", panels(tmp_path), expected_scenes=4)
    assert result["complete"] and result["size"] == [2480, 3508]
    first, second, third, fourth = result["panels"]
    assert first["bounds"][0] > second["bounds"][0]
    assert first["bounds"][1] == second["bounds"][1]
    assert third["bounds"][1] > first["bounds"][1]
    assert third["bounds"][0] > fourth["bounds"][0]
    assert all(row["text"] == "The kitten is safe." for row in result["panels"])
    assert len({row["font_size"] for row in result["panels"]}) == 1
    with Image.open(tmp_path / "comic.png") as image:
        assert image.size == (2480, 3508)
        x, y, _, _ = first["bounds"]
        assert image.getpixel((x + 100, y + 100)) == (255, 0, 0)
    assert (tmp_path / "comic.pdf").read_bytes().startswith(b"%PDF-")


def test_caption_overflow_does_not_publish_a_truncated_page(tmp_path):
    with pytest.raises(ValueError):
        render_page(tmp_path, "A rainy day", panels(tmp_path, "W" * 3000), expected_scenes=4)
    assert not (tmp_path / "comic.png").exists()
    assert not (tmp_path / "comic.pdf").exists()


def test_missing_scene_does_not_create_placeholder_artwork(tmp_path):
    with pytest.raises(ValueError):
        render_page(tmp_path, "A rainy day", panels(tmp_path)[:3], expected_scenes=4)
    assert not (tmp_path / "comic.png").exists()


def test_japanese_punctuation_stays_with_its_text_without_losing_characters():
    class FixedFont:
        def getlength(self, text):
            return len(text)

    text = "雨がやんだ。猫は「ここ」にいた。"
    lines = wrap_text(text, FixedFont(), 5)
    assert "".join(lines) == text
    assert all(not line.startswith(("。", "」")) and not line.endswith("「") for line in lines)
    assert all(len(line) <= 5 for line in lines)


def test_short_final_line_is_balanced_without_losing_text():
    class FixedFont:
        def getlength(self, text):
            return len(text)

    text = "abcdefghijk"
    lines = wrap_text(text, FixedFont(), 5)
    assert "".join(lines) == text
    assert len(lines[-1]) >= 3
    assert all(len(line) <= 5 for line in lines)


@pytest.mark.parametrize("count", [1, 2, 3, 5, 8])
def test_partial_last_page_and_multiple_pages_preserve_every_scene(tmp_path, count):
    values = panels(tmp_path)
    values = [{**values[index % 4], "scene": index + 1} for index in range(count)]
    result = render_page(tmp_path, "Ordered scenes", values, expected_scenes=count)
    assert result["reading_order"] == list(range(1, count + 1))
    assert result["page_count"] == (count + 3) // 4
    assert len(result["panels"]) == count
    assert len({row["font_size"] for row in result["panels"]}) == 1
    assert (tmp_path / "comic-2.png").exists() is (count > 4)
