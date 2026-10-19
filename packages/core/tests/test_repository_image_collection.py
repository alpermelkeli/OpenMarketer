"""Tests for collecting a repository's images: read through ``RepoFiles``, decoded, cleaned."""

import io
import random
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from openmarketer_core.repository_analysis.intake import RepoFiles
from openmarketer_core.repository_analysis.repository_images.collection import (
    collect_repository_images,
)
from openmarketer_core.repository_analysis.repository_images.preparation import (
    ImageRefused,
    prepare_image,
)
from openmarketer_core.repository_analysis.repository_images.selection import (
    ImageCandidate,
    ImageLimits,
    RepositoryImageKind,
)

LIMITS = ImageLimits()
CANDIDATE = ImageCandidate(path="assets/logo.png", kind=RepositoryImageKind.LOGO)


def png(width: int = 256, height: int = 256, *, text: str | None = None, alpha=False) -> bytes:
    mode = "RGBA" if alpha else "RGB"
    # Noise, so that the file is above the smallest size a candidate may have.
    image = Image.frombytes(mode, (width, height), random.randbytes(width * height * len(mode)))
    info = PngImagePlugin.PngInfo()
    if text is not None:
        info.add_text("Comment", text)
    out = io.BytesIO()
    image.save(out, format="PNG", pnginfo=info)
    return out.getvalue()


def jpeg_with_exif(width: int = 300, height: int = 200) -> bytes:
    image = Image.frombytes("RGB", (width, height), random.randbytes(width * height * 3))
    exif = Image.Exif()
    exif[0x010E] = "secret description"  # ImageDescription
    exif[0x0112] = 6  # Orientation: rotated 90 degrees
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif, quality=95)
    return out.getvalue()


def repository(tmp_path: Path, files: dict[str, bytes]) -> RepoFiles:
    for path, data in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return RepoFiles(tmp_path)


def test_a_png_is_written_again_without_its_text_chunks():
    prepared = prepare_image(png(text="ghp_not_a_real_token"), CANDIDATE, LIMITS)
    assert b"ghp_not_a_real_token" not in prepared.data
    with Image.open(io.BytesIO(prepared.data)) as image:
        assert "Comment" not in image.info
    assert (prepared.mime, prepared.width, prepared.height) == ("image/png", 256, 256)


def test_a_jpeg_is_turned_upright_and_written_again_without_exif():
    prepared = prepare_image(jpeg_with_exif(), CANDIDATE, LIMITS)
    assert b"secret description" not in prepared.data
    assert prepared.mime == "image/webp"
    assert (prepared.width, prepared.height) == (200, 300)
    with Image.open(io.BytesIO(prepared.data)) as image:
        assert not image.getexif()


def test_transparency_is_kept_as_png():
    assert prepare_image(png(alpha=True), CANDIDATE, LIMITS).mime == "image/png"


def test_bytes_after_the_image_are_not_kept():
    prepared = prepare_image(png() + b"appended payload", CANDIDATE, LIMITS)
    assert b"appended payload" not in prepared.data


def test_a_file_that_only_claims_to_be_an_image_is_refused():
    with pytest.raises(ImageRefused):
        prepare_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>" * 100, CANDIDATE, LIMITS)


def test_a_truncated_image_is_refused():
    with pytest.raises(ImageRefused):
        prepare_image(png()[:200], CANDIDATE, LIMITS)


def test_a_small_image_is_refused_by_its_header():
    with pytest.raises(ImageRefused, match="short side"):
        prepare_image(png(64, 512), CANDIDATE, LIMITS)


def test_collection_keeps_the_products_images_and_skips_the_rest(tmp_path):
    files = repository(
        tmp_path,
        {
            "assets/logo.png": png(),
            "screenshots/home.jpg": jpeg_with_exif(),
            "node_modules/pkg/logo.png": png(),
            "tests/fixtures/screen.png": png(),
            "assets/tiny-icon.png": png(32, 32),
            "README.md": b"# app",
        },
    )
    collected = collect_repository_images(files)
    assert [(i.candidate.path, i.candidate.kind) for i in collected.images] == [
        ("assets/logo.png", RepositoryImageKind.LOGO),
        ("screenshots/home.jpg", RepositoryImageKind.SCREENSHOT),
    ]


def test_a_file_in_which_a_secret_was_found_is_not_read(tmp_path):
    files = repository(tmp_path, {"assets/logo.png": png()})
    flagged = RepoFiles(files.root, secret_files={"assets/logo.png"})
    assert collect_repository_images(flagged).images == ()


def test_a_file_that_does_not_decode_is_counted_and_skipped(tmp_path):
    files = repository(tmp_path, {"assets/logo.png": b"\x89PNG\r\n\x1a\n" + b"0" * 4000})
    collected = collect_repository_images(files)
    assert (collected.images, collected.refused) == ((), 1)
