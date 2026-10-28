"""Compose ordered reading copies from verified saved artwork."""

import hashlib
import io
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from komaquill.infrastructure.atomic import atomic_bytes

PAGE_SIZE = (2480, 3508)
PAGE_MARGIN = 120
PANEL_GAP = 64
TITLE_HEIGHT = 160
CAPTION_PADDING = 24
BODY_FONT_SIZE = 56
MINIMUM_FONT_SIZE = 28
MIN_LAST_LINE_CHARACTERS = 3
TITLE_FONT_SIZE = 64
PDF_RESOLUTION = 300
PDF_DIMENSION_TOLERANCE = 0.01
LINE_START_FORBIDDEN = "、。，．？！：；)]）］｝〉》」』】〕"
LINE_END_FORBIDDEN = "([（［｛〈《「『【〔"


def font_path(text: str, supplied: Path | None = None) -> Path:
    """Select an installed font only if it covers every visible character."""
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = (
        [supplied]
        if supplied
        else [
            windows / "meiryo.ttc",
            windows / "NotoSansJP-VF.ttf",
            windows / "arial.ttf",
            windows / "segoeui.ttf",
            Path.home() / ".local/share/fonts/NotoSansJP-VF.ttf",
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
    )
    for path in candidates:
        if path is None or not path.is_file():
            continue
        font = ImageFont.truetype(str(path), BODY_FONT_SIZE)
        missing = font.getmask(chr(0x10FFFF))
        marker = (missing.size, bytes(missing))
        if all(
            char.isspace() or (font.getmask(char).size, bytes(font.getmask(char))) != marker
            for char in set(text)
        ):
            return path
    raise ValueError("Install a font covering the story text or supply its file path.")


def wrap_text(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Wrap without dropping characters or truncating the original narrative."""
    lines = []
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        wrapped = []
        current = ""
        for char in paragraph:
            if current and font.getlength(current + char) > width:
                move_last = char in LINE_START_FORBIDDEN or current[-1] in LINE_END_FORBIDDEN
                carry = current[-1] if move_last else ""
                line = current[:-1] if move_last else current
                if not line:
                    raise ValueError("The caption is too narrow for punctuation wrapping.")
                wrapped.append(line)
                current = carry
            if font.getlength(char) > width:
                raise ValueError("A character is wider than the available caption.")
            current += char
        wrapped.append(current)
        if len(wrapped) > 1 and len(wrapped[-1].strip()) < MIN_LAST_LINE_CHARACTERS:
            tail = wrapped[-2] + wrapped[-1]
            choices = []
            for split in range(1, len(tail)):
                left, right = tail[:split], tail[split:]
                if (
                    left[-1] not in LINE_END_FORBIDDEN
                    and right[0] not in LINE_START_FORBIDDEN
                    and len(right.strip()) >= MIN_LAST_LINE_CHARACTERS
                    and font.getlength(left) <= width
                    and font.getlength(right) <= width
                ):
                    choices.append((abs(font.getlength(left) - font.getlength(right)), split))
            if choices:
                _, split = min(choices)
                wrapped[-2:] = [tail[:split], tail[split:]]
        lines.extend(wrapped)
    return lines


def render_page(
    report: Path,
    title: str,
    panels: list[dict[str, Any]],
    *,
    expected_scenes: int,
    font: Path | None = None,
) -> dict[str, Any]:
    """Write one to eight scenes without filling absent artwork with placeholders."""
    order = list(range(1, expected_scenes + 1))
    if not 1 <= expected_scenes <= 8 or [row["scene"] for row in panels] != order:
        raise ValueError("A complete comic requires every declared scene in order.")
    pages = [panels[index : index + 4] for index in range(0, len(panels), 4)]
    chosen = font_path(title + "".join(row["text"] for row in panels), font)
    layouts = []
    for size in range(BODY_FONT_SIZE, MINIMUM_FONT_SIZE - 1, -2):
        layouts = [_layout(chunk, size, chosen) for chunk in pages]
        if all(layout[2] <= layout[3] for layout in layouts):
            break
    else:
        raise ValueError("A caption exceeds its panel; no text was truncated.")
    images, retained = [], []
    for number, (chunk, layout) in enumerate(zip(pages, layouts, strict=True), 1):
        page, rows = _draw_page(title, chunk, chosen, size, layout)
        images.append(page)
        retained.extend({**row, "page": number} for row in rows)
    outputs = {}
    for number, page in enumerate(images, 1):
        filename = "comic.png" if number == 1 else f"comic-{number}.png"
        buffer = io.BytesIO()
        page.save(buffer, format="PNG")
        content = buffer.getvalue()
        atomic_bytes(report / filename, content)
        outputs[filename] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        with Image.open(report / filename) as saved:
            saved.verify()
    buffer = io.BytesIO()
    images[0].save(
        buffer, format="PDF", save_all=True, append_images=images[1:], resolution=PDF_RESOLUTION
    )
    content = buffer.getvalue()
    atomic_bytes(report / "comic.pdf", content)
    outputs["comic.pdf"] = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    validate_pdf(report / "comic.pdf", len(images))
    return {
        "complete": True,
        "size": list(PAGE_SIZE),
        "reading_order": order,
        "page_count": len(images),
        "font_sha256": hashlib.sha256(chosen.read_bytes()).hexdigest(),
        "panels": retained,
        "outputs": outputs,
    }


def validate_pdf(path: Path, expected_pages: int) -> None:
    """Reopen every exported page and its raster before reporting completion."""
    try:
        reader = PdfReader(path, strict=True)
        if len(reader.pages) != expected_pages:
            raise ValueError("The PDF page count differs from the layout.")
        expected = tuple(value * 72 / PDF_RESOLUTION for value in PAGE_SIZE)
        for page in reader.pages:
            actual = (float(page.mediabox.width), float(page.mediabox.height))
            if any(
                abs(a - b) > PDF_DIMENSION_TOLERANCE for a, b in zip(actual, expected, strict=True)
            ):
                raise ValueError("The PDF page dimensions differ from the layout.")
            contents = page.get_contents()
            if contents is None or not contents.get_data() or len(page.images) != 1:
                raise ValueError("The PDF page artwork is missing.")
            if page.images[0].image.size != PAGE_SIZE:
                raise ValueError("The PDF raster dimensions differ from the layout.")
    except (PdfReadError, OSError, KeyError) as error:
        raise ValueError("The PDF output is invalid.") from error


def _layout(panels, size, chosen):
    panel_width = (PAGE_SIZE[0] - 2 * PAGE_MARGIN - PANEL_GAP) // 2
    body = ImageFont.truetype(str(chosen), size)
    captions = [
        wrap_text(panel["text"], body, panel_width - 2 * CAPTION_PADDING) for panel in panels
    ]
    ascent, descent = body.getmetrics()
    line_height = ascent + descent + size // 4
    row_heights = [
        panel_width
        + 2 * CAPTION_PADDING
        + max((len(lines) for lines in captions[row : row + 2]), default=0) * line_height
        for row in (0, 2)
    ]
    available = PAGE_SIZE[1] - PAGE_MARGIN - TITLE_HEIGHT - PAGE_MARGIN
    return captions, row_heights, sum(row_heights) + PANEL_GAP, available


def _draw_page(title, panels, chosen, size, layout):
    page = Image.new("RGB", PAGE_SIZE, "white")
    draw = ImageDraw.Draw(page)
    title_font = ImageFont.truetype(str(chosen), TITLE_FONT_SIZE)
    if title_font.getlength(title) > PAGE_SIZE[0] - 2 * PAGE_MARGIN:
        raise ValueError("The title exceeds the available page width.")
    draw.text((PAGE_MARGIN, PAGE_MARGIN), title, font=title_font, fill="black", anchor="lt")
    panel_width = (PAGE_SIZE[0] - 2 * PAGE_MARGIN - PANEL_GAP) // 2
    top = PAGE_MARGIN + TITLE_HEIGHT
    captions, row_heights, content_height, available_height = layout
    body = ImageFont.truetype(str(chosen), size)
    ascent, descent = body.getmetrics()
    line_height = ascent + descent + size // 4
    content_top = top + (available_height - content_height) // 2
    retained = []
    for index, panel in enumerate(panels):
        x = PAGE_MARGIN + (1 - index % 2) * (panel_width + PANEL_GAP)
        y = content_top + (row_heights[0] + PANEL_GAP if index >= 2 else 0)
        panel_height = row_heights[index // 2]
        lines = captions[index]
        with Image.open(panel["path"]) as source:
            fitted = ImageOps.contain(source.convert("RGB"), (panel_width, panel_width))
            page.paste(
                fitted,
                (x + (panel_width - fitted.width) // 2, y + (panel_width - fitted.height) // 2),
            )
        draw.rectangle((x, y, x + panel_width, y + panel_width), outline="black", width=2)
        text_y = y + panel_width + CAPTION_PADDING
        for line in lines:
            bounds = draw.textbbox((x + CAPTION_PADDING, text_y), line, font=body, anchor="lt")
            if bounds[2] > x + panel_width or bounds[3] > y + panel_height:
                raise ValueError("Caption bounds exceed the panel.")
            draw.text((x + CAPTION_PADDING, text_y), line, font=body, fill="black", anchor="lt")
            text_y += line_height
        retained.append(
            {
                "scene": panel["scene"],
                "key": panel["key"],
                "text": panel["text"],
                "font_size": size,
                "bounds": [x, y, x + panel_width, y + panel_height],
            }
        )
    return page, retained
