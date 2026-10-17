"""Specifications of which files a sandbox run left are accepted as its images."""

import io

from PIL import Image

from openmarketer_core.studio.output_check import (
    OutputLimits,
    RawOutput,
    check_outputs,
)


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (32, 24)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def test_a_png_is_accepted_with_its_size() -> None:
    (file,) = check_outputs([RawOutput("post.png", image_bytes())], OutputLimits()).files
    assert (file.mime, file.width, file.height) == ("image/png", 32, 24)


def test_a_jpeg_is_accepted() -> None:
    (file,) = check_outputs([RawOutput("a.jpg", image_bytes("JPEG"))], OutputLimits()).files
    assert file.mime == "image/jpeg"


def test_a_webp_is_accepted() -> None:
    (file,) = check_outputs([RawOutput("a.webp", image_bytes("WEBP"))], OutputLimits()).files
    assert file.mime == "image/webp"


def test_a_file_that_is_not_an_image_is_rejected() -> None:
    check = check_outputs([RawOutput("notes.txt", b"hello")], OutputLimits())
    assert (check.files, check.rejected) == (
        (),
        ("notes.txt: the file is not a PNG, JPEG or WebP image",),
    )


def test_a_gif_is_rejected() -> None:
    assert not check_outputs([RawOutput("a.gif", image_bytes("GIF"))], OutputLimits()).files


def test_a_file_with_png_magic_that_does_not_decode_is_rejected() -> None:
    broken = image_bytes()[:40]
    check = check_outputs([RawOutput("a.png", broken)], OutputLimits())
    assert check.rejected == ("a.png: the file does not decode as the image it claims to be",)


def test_a_file_over_the_byte_limit_is_rejected() -> None:
    data = image_bytes()
    check = check_outputs([RawOutput("a.png", data)], OutputLimits(max_file_bytes=len(data) - 1))
    assert check.rejected == (f"a.png: the file is larger than {len(data) - 1} bytes",)


def test_an_image_over_the_pixel_limit_is_rejected() -> None:
    check = check_outputs([RawOutput("a.png", image_bytes())], OutputLimits(max_pixels=100))
    assert check.rejected == ("a.png: the image has more than 100 pixels",)


def test_an_image_wider_than_the_side_limit_is_rejected() -> None:
    data = image_bytes(size=(300, 10))
    assert not check_outputs([RawOutput("a.png", data)], OutputLimits(max_side=200)).files


def test_files_beyond_the_count_limit_are_rejected() -> None:
    outputs = [RawOutput(f"{i}.png", image_bytes()) for i in range(3)]
    check = check_outputs(outputs, OutputLimits(max_files=2))
    assert (len(check.files), check.rejected) == (2, ("2.png: more than 2 files",))


def test_files_beyond_the_total_byte_limit_are_rejected() -> None:
    data = image_bytes()
    outputs = [RawOutput("a.png", data), RawOutput("b.png", data)]
    check = check_outputs(outputs, OutputLimits(max_total_bytes=len(data) + 1))
    assert [file.name for file in check.files] == ["a.png"]


def test_a_name_with_a_directory_is_rejected_without_repeating_it() -> None:
    check = check_outputs([RawOutput("../x.png", image_bytes())], OutputLimits())
    assert check.rejected == ("<unnamed file>: the file name is not a plain name",)


def test_an_animated_png_is_rejected() -> None:
    buffer = io.BytesIO()
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
    frames[0].save(buffer, format="PNG", save_all=True, append_images=frames[1:])
    check = check_outputs([RawOutput("a.png", buffer.getvalue())], OutputLimits())
    assert check.rejected == ("a.png: animated images are not accepted",)
