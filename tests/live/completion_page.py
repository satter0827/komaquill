"""Compose a four-panel reading copy from verified saved artwork."""

import hashlib
import io
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

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
    report: Path, title: str, panels: list[dict[str, Any]], *, font: Path | None = None
) -> dict[str, Any]:
    """Write PNG and PDF atomically after validating all four panel captions."""
    if len(panels) != 4 or [row["scene"] for row in panels] != [1, 2, 3, 4]:
        raise ValueError("A complete page requires four ordered scenes.")
    chosen = font_path(title + "".join(row["text"] for row in panels), font)
    page = Image.new("RGB", PAGE_SIZE, "white")
    draw = ImageDraw.Draw(page)
    title_font = ImageFont.truetype(str(chosen), TITLE_FONT_SIZE)
    if title_font.getlength(title) > PAGE_SIZE[0] - 2 * PAGE_MARGIN:
        raise ValueError("The title exceeds the available page width.")
    draw.text((PAGE_MARGIN, PAGE_MARGIN), title, font=title_font, fill="black", anchor="lt")
    panel_width = (PAGE_SIZE[0] - 2 * PAGE_MARGIN - PANEL_GAP) // 2
    top = PAGE_MARGIN + TITLE_HEIGHT
    available_height = PAGE_SIZE[1] - top - PAGE_MARGIN
    for size in range(BODY_FONT_SIZE, MINIMUM_FONT_SIZE - 1, -2):
        body = ImageFont.truetype(str(chosen), size)
        captions = [
            wrap_text(panel["text"], body, panel_width - 2 * CAPTION_PADDING) for panel in panels
        ]
        ascent, descent = body.getmetrics()
        line_height = ascent + descent + size // 4
        row_heights = [
            panel_width
            + 2 * CAPTION_PADDING
            + max(len(lines) for lines in captions[row : row + 2]) * line_height
            for row in (0, 2)
        ]
        content_height = sum(row_heights) + PANEL_GAP
        if content_height <= available_height:
            break
    else:
        raise ValueError("A caption exceeds its panel; no text was truncated.")
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
    outputs = {}
    for filename, format_name in (("comic.png", "PNG"), ("comic.pdf", "PDF")):
        buffer = io.BytesIO()
        page.save(buffer, format=format_name, resolution=PDF_RESOLUTION)
        content = buffer.getvalue()
        atomic_bytes(report / filename, content)
        outputs[filename] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    with Image.open(report / "comic.png") as saved:
        saved.verify()
    if not (report / "comic.pdf").read_bytes().startswith(b"%PDF-"):
        raise ValueError("The PDF output is invalid.")
    return {
        "complete": True,
        "size": list(PAGE_SIZE),
        "reading_order": [1, 2, 3, 4],
        "font_sha256": hashlib.sha256(chosen.read_bytes()).hexdigest(),
        "panels": retained,
        "outputs": outputs,
    }
