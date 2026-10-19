"""Specifications of what a square keeps when cropped to a platform's shape."""

import pytest

from openmarketer_core.studio.square_crop import (
    CROP_SAFE_LEAD,
    CentralCrop,
    central_crop_of_a_square,
    crop_safe_note,
)


def test_a_portrait_keeps_the_middle_of_the_width_at_full_height():
    assert central_crop_of_a_square(1080, 1350) == CentralCrop(width_share=0.8, height_share=1.0)


def test_a_landscape_keeps_the_full_width_and_the_middle_of_the_height():
    assert central_crop_of_a_square(1600, 900) == CentralCrop(1.0, 0.5625)


def test_a_square_keeps_the_whole_square():
    assert central_crop_of_a_square(1080, 1080).keeps_the_whole_square


def test_a_size_without_area_has_no_crop():
    with pytest.raises(ValueError):
        central_crop_of_a_square(0, 100)


def test_the_note_of_a_portrait_names_the_share_of_the_width():
    note = crop_safe_note(1080, 1350)
    assert note.startswith(CROP_SAFE_LEAD + "80% of the width: keep every word")
    assert note.endswith("with only background in the outer strips.")


def test_the_note_of_a_landscape_names_the_share_of_the_height_rounded_down():
    assert crop_safe_note(1600, 900).startswith(CROP_SAFE_LEAD + "56% of the height:")
    assert crop_safe_note(1200, 627).startswith(CROP_SAFE_LEAD + "52% of the height:")


def test_a_square_size_has_no_note():
    assert crop_safe_note(1080, 1080) == ""
