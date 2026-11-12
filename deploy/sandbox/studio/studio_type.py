"""Typography for programs the studio's model writes: the catalogue's fonts, and words that fit.

    from studio_type import fit_text, font

    font("Inter", "bold", 64)            a Pillow font of the catalogue, by family and style
    fit_text(image, "Ürünü sen yaptın.", (80, 600, 1520, 820), "League Spartan", "bold")
        draws the text, wrapped, at the largest size that fits the box; returns a ``Fitted``

The catalogue is ``typefaces.json`` beside this module, written from core's
``openmarketer_core/studio/typefaces.py`` (the one source; a test holds them equal). A program
names a family and a style, never a file. This module is part of the sandbox image, read-only,
and on the run's import path (``run_user.py``); it opens only the font files the catalogue
names and draws only on the image it is given.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

CATALOGUE = json.loads((Path(__file__).parent / "typefaces.json").read_text(encoding="utf-8"))

Box = tuple[int, int, int, int]
Colour = str | tuple[int, ...]


@dataclass(frozen=True)
class Fitted:
    """What ``fit_text`` drew: the size it used, the lines it wrapped the text into, and the
    box the drawn text takes (left, top, right, bottom)."""

    size: int
    lines: tuple[str, ...]
    bbox: Box


def families() -> list[str]:
    """The catalogue's families, by name."""
    return [typeface["name"] for typeface in CATALOGUE["typefaces"]]


def font(name: str, weight: str = "regular", size: int = 48) -> ImageFont.FreeTypeFont:
    """The catalogue family ``name`` in ``weight`` (a style: "bold", "semibold", "italic"...)
    at ``size`` pixels. Raises ``ValueError`` naming what exists for a family or style the
    catalogue lacks."""
    return ImageFont.truetype(_face(name, weight), size)


def fit_text(
    image: Image.Image,
    text: str,
    box: Box,
    font_name: str,
    weight: str = "regular",
    *,
    max_size: int | None = None,
    min_size: int = 12,
    align: str = "left",
    line_spacing: float = 1.15,
    fill: Colour = "white",
    stroke_width: int = 0,
    stroke_fill: Colour | None = None,
    shadow: Colour | None = None,
) -> Fitted:
    """Draw ``text`` in ``box`` at the largest size its wrapped lines fit, and say what it drew.

    The size is found by binary search between ``min_size`` and ``max_size`` (by default the
    box's height). Words are wrapped at spaces, never broken, and a line break in ``text`` is
    kept. ``align`` is "left", "center" or "right" within the box; the block sits at the top
    of the box. ``shadow`` is a colour for a soft drop shadow under the words. Raises
    ``ValueError`` when even ``min_size`` does not fit: shorten nothing, choose a larger box.
    """
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ValueError("the box must have a positive width and height")
    face = _face(font_name, weight)
    low, high = min_size, max_size or height
    best: tuple[int, list[str]] | None = None
    while low <= high:
        size = (low + high) // 2
        sized = ImageFont.truetype(face, size)
        lines = _wrapped(text, sized, width, stroke_width)
        if lines is not None and _block_height(lines, sized, line_spacing, stroke_width) <= height:
            best, low = (size, lines), size + 1
        else:
            high = size - 1
    if best is None:
        raise ValueError(f"the text does not fit the box even at {min_size} pixels")
    size, lines = best
    chosen = ImageFont.truetype(face, size)
    if shadow is not None:
        _draw_shadow(image, lines, chosen, box, align, line_spacing, shadow, stroke_width)
    drawn = _draw_lines(
        ImageDraw.Draw(image),
        lines,
        chosen,
        box,
        align,
        line_spacing,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )
    return Fitted(size, tuple(lines), drawn)


def _face(name: str, weight: str) -> str:
    wanted = " ".join(name.casefold().split())
    for typeface in CATALOGUE["typefaces"]:
        if " ".join(typeface["name"].casefold().split()) != wanted:
            continue
        path = typeface["faces"].get(weight.casefold())
        if path is None:
            styles = ", ".join(typeface["faces"])
            raise ValueError(f"{typeface['name']} has no style {weight!r}; it has: {styles}")
        return path
    raise ValueError(f"no font {name!r} in the catalogue; it has: {', '.join(families())}")


def _wrapped(
    text: str, chosen: ImageFont.FreeTypeFont, width: int, stroke_width: int
) -> list[str] | None:
    """``text`` in lines no wider than ``width``, or ``None`` when a word alone is wider."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if _width(candidate, chosen, stroke_width) <= width:
                line = candidate
                continue
            if not line:
                return None
            lines.append(line)
            line = word
            if _width(line, chosen, stroke_width) > width:
                return None
        lines.append(line)
    return lines


def _width(text: str, chosen: ImageFont.FreeTypeFont, stroke_width: int) -> float:
    """The width of the ink ``text`` leaves, its outline included."""
    left, _, right, _ = chosen.getbbox(text, stroke_width=stroke_width)
    return right - left


def _block_height(
    lines: list[str], sized: ImageFont.FreeTypeFont, line_spacing: float, stroke_width: int
) -> float:
    # Each line is drawn from its ascender line; the last takes the font's whole height, from
    # the top of its accents (Ü, İ) to the bottom of its descenders (g, ş, ç).
    ascent, descent = sized.getmetrics()
    return sized.size * line_spacing * (len(lines) - 1) + ascent + descent + 2 * stroke_width


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    chosen: ImageFont.FreeTypeFont,
    box: Box,
    align: str,
    line_spacing: float,
    *,
    fill: Colour,
    stroke_width: int,
    stroke_fill: Colour | None,
) -> Box:
    left, top, right, _ = box
    size = int(chosen.size)
    extent: list[float] = []
    for number, line in enumerate(lines):
        # Placed by its ink, not its advance: a letter's side bearing would push it past the
        # box's edge or leave a line looking indented.
        ink_left, _, ink_right, _ = chosen.getbbox(line, stroke_width=stroke_width)
        if align == "center":
            x = left + (right - left - (ink_right - ink_left)) / 2 - ink_left
        elif align == "right":
            x = right - ink_right
        else:
            x = left - ink_left
        y = top + number * size * line_spacing
        draw.text(
            (x, y),
            line,
            font=chosen,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        bounds = draw.textbbox((x, y), line, font=chosen, stroke_width=stroke_width)
        extent = list(bounds) if not extent else _union(extent, bounds)
    return (int(extent[0]), int(extent[1]), int(extent[2]), int(extent[3]))


def _draw_shadow(
    image: Image.Image,
    lines: list[str],
    chosen: ImageFont.FreeTypeFont,
    box: Box,
    align: str,
    line_spacing: float,
    shadow: Colour,
    stroke_width: int,
) -> None:
    offset = max(2, int(chosen.size) // 20)
    left, top, right, bottom = box
    shifted = (left + offset, top + offset, right + offset, bottom + offset)
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    _draw_lines(
        ImageDraw.Draw(layer),
        lines,
        chosen,
        shifted,
        align,
        line_spacing,
        fill=shadow,
        stroke_width=stroke_width,
        stroke_fill=shadow,
    )
    layer = layer.filter(ImageFilter.GaussianBlur(offset))
    if image.mode == "RGBA":
        image.alpha_composite(layer)
    else:
        image.paste(layer, (0, 0), layer)


def _union(extent: list[float], bounds: tuple[float, ...]) -> list[float]:
    return [
        min(extent[0], bounds[0]),
        min(extent[1], bounds[1]),
        max(extent[2], bounds[2]),
        max(extent[3], bounds[3]),
    ]
