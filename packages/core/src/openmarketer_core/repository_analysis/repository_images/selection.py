"""Which images of a repository are kept as the product's own, and what kind each one is.

A repository holds the images a product shows of itself (its app icon, its logo, store
screenshots, press and marketing images) among many it does not (fixtures, vendored
libraries, build output). This rule picks the first from paths and sizes alone, before
anything is decoded, and then chooses among the decoded ones:

- a candidate is a PNG, JPEG or WebP file (by extension; SVG is not rasterised), of
  between ``min_file_bytes`` and ``max_file_bytes``, whose path is at most
  ``MAX_PATH_LENGTH`` characters and passes through no skipped folder (dependencies,
  vendored code, tests and their fixtures, build output, hidden folders other than
  ``.github``);
- its ``kind`` is a hint read from the path: ``icon`` (an iOS app icon set, Android's
  ``mipmap-*/ic_launcher*``, a favicon, a file named ``icon*``), ``logo``, ``screenshot``
  (``screenshots/``, fastlane's ``metadata/**/images``, a store folder), ``marketing``
  (marketing, press and branding folders, documentation images) or ``other``;
- the files of one icon set are the same icon in several sizes: only the largest is kept;
- a decoded image is kept when its short side has at least ``min_short_side`` pixels and
  neither side more than ``max_side``; identical images are kept once;
- at most ``max_images`` are kept, ``per_kind`` of each kind, and ``max_total_bytes`` in
  all, preferring icons, then logos, screenshots, marketing images and the others; within
  a kind, English or unlocalised paths first, then shallower ones.

The kind is a hint from names a repository chose, so it is never more than that: nothing
here reads what an image shows. Pure functions over paths and numbers; decoding is
``preparation.py``'s.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal

# A path is stored and shown; one longer than this is not a product's image anyway.
MAX_PATH_LENGTH = 1000


# What a kept image is written again as.
StoredImageMime = Literal["image/png", "image/webp"]


class RepositoryImageKind(StrEnum):
    ICON = "icon"
    LOGO = "logo"
    SCREENSHOT = "screenshot"
    MARKETING = "marketing"
    OTHER = "other"


# The order a kind is preferred in when the caps leave room for fewer.
KIND_PRIORITY: tuple[RepositoryImageKind, ...] = tuple(RepositoryImageKind)

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})

SKIPPED_DIRS = frozenset({
    "node_modules", "bower_components", "vendor", "vendored", "third_party", "third-party",
    "external", "test", "tests", "__tests__", "testdata", "test-data", "test_data",
    "fixtures", "__fixtures__", "__snapshots__", "spec", "specs", "e2e", "coverage",
    "build", "dist", "out", "target", ".next", ".nuxt", ".gradle", ".dart_tool", "pods",
    "deriveddata", "__pycache__", ".venv", "venv", "site-packages",
})  # fmt: skip
# A hidden folder is tooling, except the one a repository's own social images live in.
KEPT_HIDDEN_DIRS = frozenset({".github"})

SCREENSHOT_DIRS = frozenset({
    "screenshots", "screenshot", "screens", "store", "appstore", "app-store", "app_store",
    "playstore", "play-store", "play_store", "store-assets", "store_assets",
})  # fmt: skip
MARKETING_DIRS = frozenset({
    "marketing", "press", "press-kit", "presskit", "press_kit", "branding", "brand",
    "media-kit", "mediakit", "promo", "promotional",
})  # fmt: skip
DOCUMENTATION_DIRS = frozenset({"docs", "doc", "documentation", "website", "site"})
DOCUMENTATION_IMAGE_DIRS = frozenset({"images", "image", "img", "assets", "media", "static"})
MARKETING_NAMES = (
    "banner", "hero", "social", "og-image", "og_image", "opengraph",
    "feature-graphic", "feature_graphic", "featuregraphic", "promo", "cover",
)  # fmt: skip
ICON_NAMES = ("icon", "app-icon", "app_icon", "appicon", "apple-touch-icon", "android-chrome")

# A folder named for a locale with its region, as fastlane and store tools name them
# (``en-US``, ``pt_BR``, ``zh-Hans``); two letters alone are a locale only when ``en``.
_LOCALE = re.compile(r"[a-z]{2}[-_](?:[a-z]{2}|hans|hant)")


def _default_per_kind() -> Mapping[RepositoryImageKind, int]:
    return MappingProxyType({
        RepositoryImageKind.ICON: 3,
        RepositoryImageKind.LOGO: 3,
        RepositoryImageKind.SCREENSHOT: 10,
        RepositoryImageKind.MARKETING: 6,
        RepositoryImageKind.OTHER: 4,
    })  # fmt: skip


@dataclass(frozen=True)
class ImageLimits:
    min_file_bytes: int = 2 * 1024
    max_file_bytes: int = 8 * 1024 * 1024
    min_short_side: int = 128
    max_side: int = 4096
    max_images: int = 24
    max_total_bytes: int = 40 * 1024 * 1024
    # The most candidates decoded for one repository, the preferred ones first.
    max_candidates: int = 120
    per_kind: Mapping[RepositoryImageKind, int] = field(default_factory=_default_per_kind)


@dataclass(frozen=True)
class ImageCandidate:
    """A file that may be one of the product's images, before it is decoded.

    ``icon_set`` names the set the file is one size of, when it is one; the files of a
    set compete for one place.
    """

    path: str
    kind: RepositoryImageKind
    icon_set: str | None = None


@dataclass(frozen=True)
class PreparedImage:
    """A candidate decoded and written again: the bytes to store and what they are."""

    candidate: ImageCandidate
    data: bytes
    mime: StoredImageMime
    width: int
    height: int
    sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def image_candidate(path: str, size_bytes: int, limits: ImageLimits) -> ImageCandidate | None:
    """``path`` as a candidate, or ``None`` when its name, place or size rules it out."""
    if len(path) > MAX_PATH_LENGTH:
        return None
    rel = PurePosixPath(path)
    if rel.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    if not limits.min_file_bytes <= size_bytes <= limits.max_file_bytes:
        return None
    dirs = [part.lower() for part in rel.parts[:-1]]
    if any(_skipped(d) for d in dirs):
        return None
    kind, icon_set = kind_of(rel)
    return ImageCandidate(path=path, kind=kind, icon_set=icon_set)


def kind_of(rel: PurePosixPath) -> tuple[RepositoryImageKind, str | None]:
    """The kind the path suggests, and the icon set it belongs to, if any."""
    dirs = [part.lower() for part in rel.parts[:-1]]
    name = rel.name.lower()
    stem = rel.stem.lower()
    parent = PurePosixPath(*rel.parts[:-1]).as_posix() if dirs else ""

    for index, d in enumerate(dirs):
        if d.endswith(".appiconset"):
            return RepositoryImageKind.ICON, PurePosixPath(*rel.parts[: index + 1]).as_posix()
    if dirs and dirs[-1].startswith("mipmap-") and stem.startswith("ic_launcher"):
        resources = PurePosixPath(*rel.parts[:-2]).as_posix() if len(dirs) > 1 else ""
        return RepositoryImageKind.ICON, f"{resources}:mipmap:{stem}"
    if stem.startswith("favicon"):
        return RepositoryImageKind.ICON, f"{parent}:favicon"
    if stem.startswith(ICON_NAMES):
        return RepositoryImageKind.ICON, None
    if "logo" in stem or any(d in ("logo", "logos") for d in dirs):
        return RepositoryImageKind.LOGO, None
    if _is_fastlane_image(dirs):
        if any(marker in name for marker in MARKETING_NAMES):
            return RepositoryImageKind.MARKETING, None
        return RepositoryImageKind.SCREENSHOT, None
    if "screenshot" in stem or any(d in SCREENSHOT_DIRS for d in dirs):
        return RepositoryImageKind.SCREENSHOT, None
    if any(d in MARKETING_DIRS for d in dirs) or _is_documentation_image(dirs):
        return RepositoryImageKind.MARKETING, None
    if any(marker in stem for marker in MARKETING_NAMES):
        return RepositoryImageKind.MARKETING, None
    return RepositoryImageKind.OTHER, None


def candidates_in_order(
    files: Iterable[tuple[str, int]], limits: ImageLimits
) -> list[ImageCandidate]:
    """The candidates among ``files`` (path and size), preferred first, up to ``max_candidates``."""
    found = [
        candidate
        for path, size_bytes in files
        if (candidate := image_candidate(path, size_bytes, limits)) is not None
    ]
    return sorted(found, key=_preference)[: limits.max_candidates]


def size_refusal(width: int, height: int, limits: ImageLimits) -> str | None:
    """Why an image of ``width`` by ``height`` pixels is not kept, or ``None`` when it is."""
    if min(width, height) < limits.min_short_side:
        return f"its short side is under {limits.min_short_side} pixels"
    if max(width, height) > limits.max_side:
        return f"it is larger than {limits.max_side} pixels a side"
    return None


def chosen_images(prepared: Sequence[PreparedImage], limits: ImageLimits) -> list[PreparedImage]:
    """The images kept of ``prepared``, preferred first, within every cap."""
    largest_of_set: dict[str, PreparedImage] = {}
    for image in prepared:
        icon_set = image.candidate.icon_set
        if icon_set is None:
            continue
        held = largest_of_set.get(icon_set)
        if held is None or _area(image) > _area(held):
            largest_of_set[icon_set] = image

    kept: list[PreparedImage] = []
    seen: set[str] = set()
    per_kind: dict[RepositoryImageKind, int] = {}
    total = 0
    for image in sorted(prepared, key=lambda image: _preference(image.candidate)):
        candidate = image.candidate
        if candidate.icon_set is not None and largest_of_set[candidate.icon_set] is not image:
            continue
        if image.sha256 in seen or len(kept) >= limits.max_images:
            continue
        if per_kind.get(candidate.kind, 0) >= limits.per_kind.get(candidate.kind, 0):
            continue
        if total + image.size_bytes > limits.max_total_bytes:
            continue
        kept.append(image)
        seen.add(image.sha256)
        per_kind[candidate.kind] = per_kind.get(candidate.kind, 0) + 1
        total += image.size_bytes
    return kept


def _skipped(directory: str) -> bool:
    if directory in SKIPPED_DIRS:
        return True
    return directory.startswith(".") and directory not in KEPT_HIDDEN_DIRS


def _is_fastlane_image(dirs: list[str]) -> bool:
    if "fastlane" not in dirs:
        return False
    after = dirs[dirs.index("fastlane") + 1 :]
    return "metadata" in after and "images" in after


def _is_documentation_image(dirs: list[str]) -> bool:
    return (
        bool(dirs)
        and dirs[0] in DOCUMENTATION_DIRS
        and any(d in DOCUMENTATION_IMAGE_DIRS for d in dirs[1:])
    )


def _preference(candidate: ImageCandidate) -> tuple[int, int, int, str]:
    rel = PurePosixPath(candidate.path)
    return (
        KIND_PRIORITY.index(candidate.kind),
        0 if _english_or_unlocalised(rel) else 1,
        len(rel.parts),
        candidate.path,
    )


def _english_or_unlocalised(rel: PurePosixPath) -> bool:
    for part in (p.lower() for p in rel.parts[:-1]):
        if part == "en" or _LOCALE.fullmatch(part):
            return part.startswith("en")
    return True


def _area(image: PreparedImage) -> int:
    return image.width * image.height
