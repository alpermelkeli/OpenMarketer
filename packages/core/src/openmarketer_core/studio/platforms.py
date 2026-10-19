"""The places an asset of the creative studio is made for, and what each asks of an image.

A platform is a closed list: each slug names one placement (an Instagram post, an X post, an
Open Graph image) and its ``PlatformSpec``: the size in pixels the asset is made at, the margin
kept clear of text and logos on every side, the most bytes the file may have and the formats
it may be in. The programmatic checks measure a candidate against it; the request names one
(``requests.py``); the database checks a stored slug against the same list (``db/models.py``).

The values are this installation's, chosen to sit inside what each platform accepts: a file
smaller than its upload limit, a format every client of it shows. The margin is one number for
all four sides, so for a story it is set by the overlays at the top and the bottom and is
wider at the sides than it needs to be. Nothing here knows a platform's API or posts anything.

This module reaches no database and no network.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from openmarketer_core.studio.sandbox import ImageMime

_MIB = 1024 * 1024


class Platform(StrEnum):
    INSTAGRAM_POST = "instagram_post"
    INSTAGRAM_PORTRAIT = "instagram_portrait"
    INSTAGRAM_STORY = "instagram_story"
    X_POST = "x_post"
    LINKEDIN_POST = "linkedin_post"
    OG_IMAGE = "og_image"
    APP_STORE_SCREENSHOT = "app_store_screenshot"


@dataclass(frozen=True)
class PlatformSpec:
    """What one platform asks of an image: its size, its clear margin, its bytes and formats."""

    width: int
    height: int
    safe_margin_px: int
    max_bytes: int
    formats: tuple[ImageMime, ...]

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height


_PNG_OR_JPEG: tuple[ImageMime, ...] = ("image/png", "image/jpeg")

PLATFORMS: Mapping[Platform, PlatformSpec] = MappingProxyType(
    {
        Platform.INSTAGRAM_POST: PlatformSpec(1080, 1080, 60, 8 * _MIB, _PNG_OR_JPEG),
        Platform.INSTAGRAM_PORTRAIT: PlatformSpec(1080, 1350, 60, 8 * _MIB, _PNG_OR_JPEG),
        # The account's name at the top and the reply bar at the bottom cover about this much.
        Platform.INSTAGRAM_STORY: PlatformSpec(1080, 1920, 220, 8 * _MIB, _PNG_OR_JPEG),
        Platform.X_POST: PlatformSpec(
            1600, 900, 60, 5 * _MIB, ("image/png", "image/jpeg", "image/webp")
        ),
        Platform.LINKEDIN_POST: PlatformSpec(1200, 627, 48, 5 * _MIB, _PNG_OR_JPEG),
        Platform.OG_IMAGE: PlatformSpec(1200, 630, 60, 5 * _MIB, _PNG_OR_JPEG),
        # The status bar and the home indicator of the device the screenshot is shown on.
        Platform.APP_STORE_SCREENSHOT: PlatformSpec(1290, 2796, 120, 10 * _MIB, _PNG_OR_JPEG),
    }
)


def platform_spec(platform: Platform) -> PlatformSpec:
    """What ``platform`` asks of an image."""
    return PLATFORMS[platform]
