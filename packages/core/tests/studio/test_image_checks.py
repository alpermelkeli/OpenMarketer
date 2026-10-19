"""Tests for the programmatic checks of a studio candidate: its file, and the words it shows."""

import io

from PIL import Image

from openmarketer_core.studio.image_checks import (
    TOO_MANY_BYTES,
    UNREADABLE,
    WRONG_FORMAT,
    WRONG_SIZE,
    Unconfirmed,
    check_image,
    copy_matches,
)
from openmarketer_core.studio.platforms import Platform, PlatformSpec, platform_spec

POST = platform_spec(Platform.INSTAGRAM_POST)


def an_image(width: int, height: int, format: str = "PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (20, 40, 60)).save(out, format=format)
    return out.getvalue()


def test_an_image_at_the_platform_size_and_format_passes():
    checked = check_image(an_image(1080, 1080), POST)
    assert checked.passed
    assert (checked.width, checked.height, checked.mime) == (1080, 1080, "image/png")


def test_an_image_of_another_size_fails_on_size():
    assert check_image(an_image(1080, 1000), POST).failures == (WRONG_SIZE,)


def test_a_format_the_platform_does_not_take_fails_on_format():
    assert check_image(an_image(1080, 1080, "WEBP"), POST).failures == (WRONG_FORMAT,)


def test_a_file_over_the_platform_limit_fails_on_bytes():
    tiny = PlatformSpec(1080, 1080, 60, 100, ("image/png",))
    assert check_image(an_image(1080, 1080), tiny).failures == (TOO_MANY_BYTES,)


def test_bytes_that_are_not_an_image_are_unreadable():
    assert check_image(b"<svg></svg>", POST).failures == (UNREADABLE,)


def test_copy_matches_whatever_the_case_punctuation_and_line_breaks():
    checked = copy_matches("LAUNCH\nday!  Try   it — free", ["Launch day", "Try it, free."])
    assert checked.all_confirmed
    assert checked.confirmed == ("Launch day", "Try it, free.")


def test_a_line_read_differently_is_unconfirmed_with_what_was_read():
    checked = copy_matches("Lauch day is here", ["Launch day"])
    assert checked.unconfirmed == (Unconfirmed("Launch day", "lauch day"),)
    assert checked.confirmed == ()


def test_a_line_nothing_reads_like_is_unconfirmed_with_nothing_read():
    checked = copy_matches("Something else entirely", ["Launch day"])
    assert checked.unconfirmed == (Unconfirmed("Launch day", ""),)


def test_no_copy_asked_for_confirms_whatever_was_read():
    assert copy_matches("anything", []).all_confirmed


def test_a_line_is_not_found_inside_a_longer_word():
    assert not copy_matches("relaunched", ["launch"]).all_confirmed


def test_a_line_read_with_short_noise_between_its_words_is_confirmed():
    check = copy_matches("Soruyu çek, o\nkasana at. üne\n\nSoruKasam", ["Soruyu çek, kasana at."])
    assert check.all_confirmed


def test_noise_does_not_excuse_a_misread_word():
    check = copy_matches("Soruyu cek, o\nkasanna at.", ["Soruyu çek, kasana at."])
    assert not check.all_confirmed


def test_a_long_word_between_the_words_of_a_line_is_not_noise():
    check = copy_matches("Soruyu çek, hemen kasana at.", ["Soruyu çek, kasana at."])
    assert not check.all_confirmed


def test_a_line_read_without_its_marks_is_left_for_the_verifier_to_read():
    check = copy_matches(
        "Soruyu cek,\nkasana at.\nSinavdan once tekrar et",
        [
            "Soruyu çek, kasana at.",
            "Sınavdan önce tekrar et",
        ],
    )
    assert [line.expected for line in check.unconfirmed] == [
        "Soruyu çek, kasana at.",
        "Sınavdan önce tekrar et",
    ]
    assert check.unconfirmed[0].read == "soruyu cek kasana at"


def test_letters_out_of_order_are_unconfirmed_without_marks():
    check = copy_matches("Soryuu cek, kasana at.", ["Soruyu çek, kasana at."])
    assert check.unconfirmed == (Unconfirmed("Soruyu çek, kasana at.", "soryuu cek kasana at"),)


def test_the_check_data_names_the_confirmed_lines_and_what_ocr_read_of_the_others():
    check = copy_matches("Launch day\nTry it fre", ["Launch day", "Try it free"])
    assert check.as_data() == {
        "confirmed": ["Launch day"],
        "unconfirmed": [{"expected": "Try it free", "read": "try it fre"}],
    }
