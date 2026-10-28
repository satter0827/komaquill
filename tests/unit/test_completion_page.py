"""Check exported panel order, complete text and bounded page layout."""

import pytest
from PIL import Image

from tests.live.completion_page import render_page, validate_pdf, wrap_text


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


def test_pdf_validation_detects_truncation_missing_page_and_wrong_size(tmp_path):
    render_page(tmp_path, "A rainy day", panels(tmp_path), expected_scenes=4)
    path = tmp_path / "comic.pdf"
    with pytest.raises(ValueError, match="page count"):
        validate_pdf(path, 2)
    path.write_bytes(path.read_bytes()[:200])
    with pytest.raises(ValueError):
        validate_pdf(path, 1)
    Image.new("RGB", (100, 100)).save(path, "PDF", resolution=300)
    with pytest.raises(ValueError, match="dimensions"):
        validate_pdf(path, 1)


def test_pdf_decodes_all_pages_in_the_same_order_as_png(tmp_path):
    from pypdf import PdfReader

    values = panels(tmp_path)
    values += [{**row, "scene": row["scene"] + 4} for row in values]
    layout = render_page(tmp_path, "Eight scenes", values, expected_scenes=8)
    reader = PdfReader(tmp_path / "comic.pdf", strict=True)
    assert len(reader.pages) == 2
    for position, page in enumerate(reader.pages, 1):
        image = page.images[0].image.convert("RGB")
        for row in layout["panels"]:
            if row["page"] == position:
                x, y, _, _ = row["bounds"]
                with Image.open(
                    tmp_path / ("comic.png" if position == 1 else "comic-2.png")
                ) as png:
                    expected = png.getpixel((x + 100, y + 100))
                assert all(
                    abs(a - b) < 8
                    for a, b in zip(image.getpixel((x + 100, y + 100)), expected, strict=True)
                )
