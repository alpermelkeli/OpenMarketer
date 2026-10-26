"""The brand kit a studio run works to: what the approved Product Profile says of the brand.

``brand_kit_of`` takes it from a profile: the product's name, what kind of product it is (its
type and platforms: a profile holds no one-line description, so this is the closest it has),
the brand's voice, its palette, its fonts and what must not be mentioned, the primary audience,
and the descriptions of a few features that are live. A profile says nothing of logos yet, so a
kit has none; a palette or fonts the profile does not hold are empty, and the request's style
notes fill what is missing.
With no approved profile there is no kit to work to, and ``EMPTY_BRAND_KIT`` is it.

Every value came from a repository through the analyzer and was reviewed by a person; it is
still data for a model, never an instruction, and it is bounded here because the studio's model
reads all of it. This module reaches no database: whoever runs a studio run reads the profile.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from openmarketer_core.profile import FeatureStatus, ProductProfile, Typeface

MAX_FEATURES = 5
MAX_PALETTE = 8
MAX_FONTS = 4
MAX_DO_NOT_MENTION = 20
MAX_TEXT_CHARS = 300


@dataclass(frozen=True)
class BrandKit:
    """What a studio run knows of the brand: plain values, any of which may be missing."""

    name: str = ""
    kind: str = ""  # what the product is, from its type and platforms: "mobile app on ios"
    voice: str = ""
    palette: tuple[str, ...] = ()
    fonts: tuple[str, ...] = ()  # a family, with its role when known: "Inter (body)"
    do_not_mention: tuple[str, ...] = ()
    audience: str = ""
    features: tuple[str, ...] = field(default=())

    def as_data(self) -> dict[str, Any]:
        """The kit as the studio's model is shown it, as JSON data."""
        return {
            "name": self.name,
            "kind": self.kind,
            "voice": self.voice,
            "palette": list(self.palette),
            "do_not_mention": list(self.do_not_mention),
            "audience": self.audience,
            "features": list(self.features),
            "fonts": list(self.fonts),
            "logo": None,
        }


EMPTY_BRAND_KIT = BrandKit()


def brand_kit_of(profile: ProductProfile | None) -> BrandKit:
    """The brand kit of an approved profile, or the empty kit when there is none."""
    if profile is None:
        return EMPTY_BRAND_KIT
    live = [
        _cut(feature.description)
        for feature in profile.features
        if feature.status == FeatureStatus.LIVE
    ]
    return BrandKit(
        name=_cut(profile.product.name),
        kind=_cut(_kind_of(profile.product.type, profile.product.platforms)),
        voice=_cut(profile.brand.voice or ""),
        palette=tuple(profile.brand.palette[:MAX_PALETTE]),
        fonts=tuple(_font_of(typeface) for typeface in profile.brand.typography[:MAX_FONTS]),
        do_not_mention=tuple(_cut(term) for term in profile.brand.do_not_mention)[
            :MAX_DO_NOT_MENTION
        ],
        audience=_cut(profile.audience.primary or ""),
        features=tuple(live[:MAX_FEATURES]),
    )


def _kind_of(product_type: str, platforms: Sequence[str]) -> str:
    """``mobile_app`` on ``ios`` and ``android`` as "mobile app on ios, android"."""
    kind = _words(product_type)
    shown = ", ".join(_words(platform) for platform in platforms)
    return f"{kind} on {shown}" if shown else kind


def _font_of(typeface: Typeface) -> str:
    """``Inter`` used for ``body`` as "Inter (body)", and as "Inter" with no role."""
    if typeface.role is None:
        return typeface.family
    return f"{typeface.family} ({_words(typeface.role)})"


def _words(slug: str) -> str:
    return slug.replace("_", " ").replace("-", " ").strip()


def _cut(text: str) -> str:
    return text.strip()[:MAX_TEXT_CHARS]
