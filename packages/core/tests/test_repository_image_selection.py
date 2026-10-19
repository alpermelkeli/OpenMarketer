"""Tests for the rule of which repository images are the product's own, and their kinds."""

import hashlib
from dataclasses import replace

import pytest

from openmarketer_core.repository_analysis.repository_images.selection import (
    MAX_PATH_LENGTH,
    ImageCandidate,
    ImageLimits,
    PreparedImage,
    RepositoryImageKind,
    candidates_in_order,
    chosen_images,
    image_candidate,
    size_refusal,
)

LIMITS = ImageLimits()
SIZE = 50_000

ICON = RepositoryImageKind.ICON
LOGO = RepositoryImageKind.LOGO
SCREENSHOT = RepositoryImageKind.SCREENSHOT
MARKETING = RepositoryImageKind.MARKETING
OTHER = RepositoryImageKind.OTHER


def kind(path: str) -> RepositoryImageKind | None:
    found = image_candidate(path, SIZE, LIMITS)
    return None if found is None else found.kind


def prepared(
    path: str, *, width: int = 512, height: int = 512, size_bytes: int = 1000, data: bytes = b""
) -> PreparedImage:
    candidate = image_candidate(path, SIZE, LIMITS)
    assert candidate is not None
    body = data or path.encode().ljust(size_bytes, b".")
    return PreparedImage(
        candidate=candidate,
        data=body,
        mime="image/png",
        width=width,
        height=height,
        sha256=hashlib.sha256(body).hexdigest(),
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("ios/App/Assets.xcassets/AppIcon.appiconset/Icon-1024.png", ICON),
        ("android/app/src/main/res/mipmap-xxxhdpi/ic_launcher.png", ICON),
        ("public/favicon-512.png", ICON),
        ("assets/icon.png", ICON),
        ("public/apple-touch-icon.png", ICON),
        ("assets/brand/logo-dark.png", LOGO),
        ("static/logos/wordmark.webp", LOGO),
        ("screenshots/home.png", SCREENSHOT),
        ("fastlane/metadata/android/en-US/images/phoneScreenshots/1.png", SCREENSHOT),
        ("store/listing-1.jpg", SCREENSHOT),
        ("docs/home-screenshot.jpeg", SCREENSHOT),
        ("assets/ipad-ss/App Store 12.png", SCREENSHOT),
        ("assets/iphone_ss/home.png", SCREENSHOT),
        ("assets/App Store 3.png", SCREENSHOT),
        ("assets/icon/foreground.png", ICON),
        ("fastlane/metadata/android/en-US/images/featureGraphic.png", MARKETING),
        ("assets/marketing/launch.png", MARKETING),
        ("press/kit-photo.jpg", MARKETING),
        ("docs/images/overview.png", MARKETING),
        ("readme-banner.png", MARKETING),
        ("src/assets/illustration.png", OTHER),
    ],
)
def test_the_kind_follows_from_the_path(path, expected):
    assert kind(path) is expected


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/pkg/logo.png",
        "vendor/lib/icon.png",
        "third_party/sdk/screenshot.png",
        "tests/fixtures/logo.png",
        "src/__tests__/screen.png",
        "spec/support/logo.png",
        "build/outputs/logo.png",
        "dist/icon.png",
        ".git/logo.png",
        ".idea/icon.png",
    ],
)
def test_a_file_in_a_skipped_folder_is_no_candidate(path):
    assert kind(path) is None


def test_a_hidden_github_folder_is_read():
    assert kind(".github/social-preview.png") is MARKETING


@pytest.mark.parametrize("path", ["logo.svg", "logo.gif", "logo.ico", "logo.pdf", "logo"])
def test_only_png_jpeg_and_webp_are_candidates(path):
    assert kind(path) is None


def test_upper_case_extensions_are_candidates():
    assert kind("assets/Logo.PNG") is LOGO


@pytest.mark.parametrize("size_bytes", [LIMITS.min_file_bytes - 1, LIMITS.max_file_bytes + 1])
def test_a_file_outside_the_byte_bounds_is_no_candidate(size_bytes):
    assert image_candidate("assets/logo.png", size_bytes, LIMITS) is None


def test_a_path_longer_than_the_column_is_no_candidate():
    long = "a/" * (MAX_PATH_LENGTH // 2) + "logo.png"
    assert image_candidate(long, SIZE, LIMITS) is None


def test_files_of_one_icon_set_share_the_set():
    folder = "ios/App/Assets.xcassets/AppIcon.appiconset"
    small = image_candidate(f"{folder}/Icon-40.png", SIZE, LIMITS)
    large = image_candidate(f"{folder}/Icon-1024.png", SIZE, LIMITS)
    assert small is not None and large is not None
    assert small.icon_set == large.icon_set == folder


def test_android_densities_of_one_launcher_icon_share_a_set():
    res = "android/app/src/main/res"
    sets = {
        candidate.icon_set
        for density in ("mdpi", "xxxhdpi")
        if (candidate := image_candidate(f"{res}/mipmap-{density}/ic_launcher.png", SIZE, LIMITS))
    }
    assert len(sets) == 1


def test_candidates_come_preferred_first():
    files = [
        ("src/a.png", SIZE),
        ("press/b.png", SIZE),
        ("screenshots/c.png", SIZE),
        ("logo.png", SIZE),
        ("icon.png", SIZE),
    ]
    assert [c.kind for c in candidates_in_order(files, LIMITS)] == [
        ICON,
        LOGO,
        SCREENSHOT,
        MARKETING,
        OTHER,
    ]


def test_english_screenshots_come_before_other_locales():
    base = "fastlane/metadata/android/{}/images/phoneScreenshots/1.png"
    files = [(base.format(locale), SIZE) for locale in ("de-DE", "en-US", "pt_BR")]
    assert candidates_in_order(files, LIMITS)[0].path == base.format("en-US")


def test_no_more_candidates_are_decoded_than_the_limit():
    files = [(f"src/{n}.png", SIZE) for n in range(10)]
    assert len(candidates_in_order(files, replace(LIMITS, max_candidates=3))) == 3


@pytest.mark.parametrize(("width", "height"), [(127, 512), (512, 127), (4097, 512), (512, 4097)])
def test_an_image_outside_the_pixel_bounds_is_refused(width, height):
    assert size_refusal(width, height, LIMITS) is not None


def test_an_image_within_the_pixel_bounds_is_kept():
    assert size_refusal(128, 4096, LIMITS) is None


def test_only_the_largest_of_an_icon_set_is_kept():
    folder = "ios/App/Assets.xcassets/AppIcon.appiconset"
    small = prepared(f"{folder}/Icon-180.png", width=180, height=180)
    large = prepared(f"{folder}/Icon-1024.png", width=1024, height=1024)
    assert chosen_images([small, large], LIMITS) == [large]


def test_identical_images_are_kept_once():
    first = prepared("screenshots/a.png", data=b"same image")
    second = prepared("screenshots/b.png", data=b"same image")
    assert chosen_images([first, second], LIMITS) == [first]


def test_each_kind_is_capped():
    shots = [prepared(f"screenshots/{n:02}.png") for n in range(15)]
    kept = chosen_images(shots, LIMITS)
    assert len(kept) == LIMITS.per_kind[SCREENSHOT]


def test_the_total_count_is_capped_preferring_icons_logos_and_screenshots():
    images = [
        *(prepared(f"src/other-{n}.png") for n in range(4)),
        *(prepared(f"press/m-{n}.png") for n in range(6)),
        *(prepared(f"screenshots/s-{n:02}.png") for n in range(10)),
        *(prepared(f"assets/logo-{n}.png") for n in range(3)),
        prepared("assets/icon.png"),
    ]
    kept = chosen_images(images, replace(LIMITS, max_images=12))
    assert [image.candidate.kind for image in kept] == [ICON] + [LOGO] * 3 + [SCREENSHOT] * 8


def test_the_total_bytes_are_capped():
    images = [prepared(f"screenshots/{n}.png", size_bytes=400) for n in range(5)]
    kept = chosen_images(images, replace(LIMITS, max_total_bytes=1000))
    assert len(kept) == 2


def test_a_candidate_names_no_icon_set_unless_it_is_one():
    assert image_candidate("assets/logo.png", SIZE, LIMITS) == ImageCandidate(
        path="assets/logo.png", kind=LOGO
    )
