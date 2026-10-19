"""Tests for the contact sheet of a product's repository images."""

import io

from PIL import Image, ImageOps

from openmarketer_core.studio.contact_sheet import (
    COLUMNS,
    GAP_PX,
    LABEL_PX,
    MAX_LABEL_CHARS,
    THUMB_PX,
    SheetImage,
    contact_sheet,
    sheet_size,
    short_path,
)


def a_png(width: int, height: int, colour=(20, 120, 220, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


def an_image(number: int, data: bytes | None = None) -> SheetImage:
    return SheetImage(
        number=number,
        kind="icon",
        path=f"ios/App/Assets.xcassets/AppIcon.appiconset/icon-{number}.png",
        data=a_png(1024, 512) if data is None else data,
    )


def opened(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def test_the_sheet_is_as_large_as_the_number_of_images_says():
    cell_w, cell_h = THUMB_PX + GAP_PX, THUMB_PX + LABEL_PX + GAP_PX

    assert sheet_size(1) == (GAP_PX + cell_w, GAP_PX + cell_h)
    assert sheet_size(COLUMNS) == (GAP_PX + COLUMNS * cell_w, GAP_PX + cell_h)
    assert sheet_size(COLUMNS + 1) == (GAP_PX + COLUMNS * cell_w, GAP_PX + 2 * cell_h)
    assert sheet_size(24) == (GAP_PX + COLUMNS * cell_w, GAP_PX + 4 * cell_h)


def test_a_sheet_is_a_png_of_its_size_and_the_same_images_draw_the_same_sheet():
    images = [an_image(n) for n in range(1, 8)]

    sheet = contact_sheet(images)

    assert opened(sheet).size == sheet_size(7)
    assert contact_sheet(images) == sheet


def test_each_thumbnail_has_its_label_drawn_under_it():
    sheet = opened(contact_sheet([an_image(1)]))

    label = sheet.crop((GAP_PX, GAP_PX + THUMB_PX, GAP_PX + THUMB_PX, GAP_PX + THUMB_PX + LABEL_PX))
    inked = ImageOps.invert(label).getbbox()  # the box of what is not white
    assert inked is not None and inked[2] - inked[0] > 40
    thumbnail = sheet.getpixel((GAP_PX + THUMB_PX // 2, GAP_PX + THUMB_PX // 2))
    assert thumbnail == (20, 120, 220)


def test_a_long_path_is_shortened_to_its_end():
    short = short_path("a/" * 40 + "icon-1024.png")

    assert len(short) == MAX_LABEL_CHARS
    assert short.startswith("...") and short.endswith("icon-1024.png")
    assert short_path("logo.png") == "logo.png"


def test_an_image_that_does_not_decode_is_drawn_as_unreadable_and_the_sheet_still_made():
    sheet = contact_sheet([an_image(1, b"not an image"), an_image(2)])

    assert opened(sheet).size == sheet_size(2)


def test_an_image_that_is_not_there_is_drawn_as_unreadable():
    gone = SheetImage(number=1, kind="logo", path="logo.png", data=None)

    assert opened(contact_sheet([gone])).size == sheet_size(1)
