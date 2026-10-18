"""The contact sheet of a product's repository images: one image a model can read them all in.

The creative director is shown the images an analysis kept of the product (its icon, its logo,
its screenshots) on one sheet instead of one image each: numbered thumbnails in a grid, each
with its number and kind and a short form of its path drawn under it, in the order the list
it is shown beside gives them. The sheet's size depends on the number of images alone
(``sheet_size``), so the same list makes a sheet of the same size every time.

What is drawn is data: a path is the repository's text, and a thumbnail may show words; the
prompt that shows the sheet says so. An image that does not decode is drawn as a plain tile
marked unreadable rather than failing the sheet. This module decides nothing about which
images there are or what they are for; it only draws.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

COLUMNS = 6
THUMB_PX = 192
LABEL_PX = 40
GAP_PX = 8
LABEL_FONT_PX = 14
# The most of a path drawn under its thumbnail: its end, which names the file.
MAX_LABEL_CHARS = 26
BACKGROUND = (255, 255, 255)
TILE = (236, 236, 236)
INK = (32, 32, 32)
FAINT = (110, 110, 110)


@dataclass(frozen=True)
class SheetImage:
    """One image of the sheet: its number in the list, its kind and path, its bytes."""

    number: int
    kind: str
    path: str
    data: bytes | None = field(repr=False)  # None when the image could not be read


def sheet_size(count: int) -> tuple[int, int]:
    """The width and height of the sheet of ``count`` images."""
    columns = max(1, min(count, COLUMNS))
    rows = max(1, -(-count // COLUMNS))
    return (
        GAP_PX + columns * (THUMB_PX + GAP_PX),
        GAP_PX + rows * (THUMB_PX + LABEL_PX + GAP_PX),
    )


def short_path(path: str) -> str:
    """The end of ``path`` that fits under a thumbnail."""
    flat = " ".join(path.split())
    if len(flat) <= MAX_LABEL_CHARS:
        return flat
    return "..." + flat[-(MAX_LABEL_CHARS - 3) :]


def contact_sheet(images: Sequence[SheetImage]) -> bytes:
    """The sheet of ``images`` as a PNG, in their order, left to right and top to bottom."""
    sheet = Image.new("RGB", sheet_size(len(images)), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(LABEL_FONT_PX)
    for position, image in enumerate(images):
        left = GAP_PX + (position % COLUMNS) * (THUMB_PX + GAP_PX)
        top = GAP_PX + (position // COLUMNS) * (THUMB_PX + LABEL_PX + GAP_PX)
        draw.rectangle((left, top, left + THUMB_PX - 1, top + THUMB_PX - 1), fill=TILE)
        thumbnail = _thumbnail(image.data)
        if thumbnail is None:
            draw.text((left + 8, top + THUMB_PX // 2 - 8), "unreadable", fill=FAINT, font=font)
        else:
            x = left + (THUMB_PX - thumbnail.width) // 2
            y = top + (THUMB_PX - thumbnail.height) // 2
            sheet.paste(thumbnail, (x, y), thumbnail)
        label_top = top + THUMB_PX + 2
        draw.text((left, label_top), f"{image.number} - {image.kind}", fill=INK, font=font)
        draw.text((left, label_top + 18), short_path(image.path), fill=FAINT, font=font)
    out = io.BytesIO()
    sheet.save(out, format="PNG")
    return out.getvalue()


def _thumbnail(data: bytes | None) -> Image.Image | None:
    """The image fitted inside a tile, with its transparency, or ``None`` when it does not read."""
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = opened.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    image.thumbnail((THUMB_PX, THUMB_PX))
    return image
