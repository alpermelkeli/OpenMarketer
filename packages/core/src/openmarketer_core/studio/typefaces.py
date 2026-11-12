"""The typefaces the studio's sandbox has, and how the studio's models are told to use them.

The sandbox image installs a short, curated set of open-licence fonts from Debian packages
(``deploy/sandbox/studio/Dockerfile``); this catalogue is the one source of what they are: each
family's name (the one fontconfig, Chromium and CSS know), its category, the mood it suits, the
Debian package it comes from, and the file of each weight in the image. Every face covers the
Turkish letters (``TURKISH_LETTERS``) and the other Latin letters the studio's copy uses.

From it come:

- ``catalogue_data``: the catalogue as JSON data, which the sandbox's typography helper
  (``deploy/sandbox/studio/studio_type.py``) reads from ``typefaces.json`` beside it. That file
  is written from here: ``uv run python -m openmarketer_core.studio.typefaces >
  deploy/sandbox/studio/typefaces.json``; a test holds the two equal, and another that the
  Dockerfile installs every package named here.
- ``typography_guide``: the short guide the studio's prompts carry (``rules.py``).
- ``nearest_typeface``: the catalogue family to use for a brand font the sandbox does not have.

It is data and pure functions: it reads no file and knows nothing of the sandbox's runs.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Category(StrEnum):
    DISPLAY = "display"  # headlines: a few large words
    SANS = "sans"  # clean sans serif: text, and headlines in a heavy weight
    SERIF = "serif"
    MONO = "mono"


# The styles a face may be asked for, lightest first, then the italics.
STYLES: tuple[str, ...] = (
    "thin",
    "extralight",
    "light",
    "regular",
    "medium",
    "semibold",
    "bold",
    "extrabold",
    "black",
    "italic",
    "bold-italic",
)

# Every face of the catalogue renders these (checked in the built image).
TURKISH_LETTERS = "ğĞşŞıİçÇöÖüÜ"


@dataclass(frozen=True)
class Typeface:
    """One family of the catalogue: ``faces`` maps a style of ``STYLES`` to its file."""

    name: str
    category: Category
    package: str
    mood: str
    faces: Mapping[str, str]

    @property
    def styles(self) -> tuple[str, ...]:
        """The styles this family has, in the order of ``STYLES``."""
        return tuple(style for style in STYLES if style in self.faces)


def _faces(directory: str, files: Mapping[str, str]) -> dict[str, str]:
    return {style: f"{directory}/{file}" for style, file in files.items()}


_LEAGUE_SPARTAN = "/usr/share/fonts/opentype/league-spartan"
_BEBAS = "/usr/share/fonts/opentype/bebas-neue"
_SORA = "/usr/share/fonts/opentype/sora"
_MANROPE = "/usr/share/fonts/truetype/manrope"
_COMFORTAA = "/usr/share/fonts/truetype/comfortaa"
_INTER = "/usr/share/fonts/opentype/inter"
_OPEN_SANS = "/usr/share/fonts/truetype/open-sans"
_ROBOTO = "/usr/share/fonts/truetype/roboto/unhinted"
_LATO = "/usr/share/fonts/truetype/lato"
_NOTO = "/usr/share/fonts/truetype/noto"
_GARAMOND = "/usr/share/fonts/opentype/ebgaramond"
_VOLLKORN = "/usr/share/fonts/truetype/vollkorn"
_ROBOTO_SLAB = "/usr/share/fonts/opentype/roboto/slab"
_JETBRAINS = "/usr/share/fonts/truetype/jetbrains-mono"
_FIRA_CODE = "/usr/share/fonts/truetype/firacode"

TYPEFACES: tuple[Typeface, ...] = (
    Typeface(
        "League Spartan",
        Category.DISPLAY,
        "fonts-league-spartan",
        "geometric and bold: confident launch and product headlines",
        _faces(
            _LEAGUE_SPARTAN,
            {
                "extralight": "LeagueSpartan-ExtraLight.otf",
                "light": "LeagueSpartan-Light.otf",
                "regular": "LeagueSpartan-Regular.otf",
                "medium": "LeagueSpartan-Medium.otf",
                "semibold": "LeagueSpartan-SemiBold.otf",
                "bold": "LeagueSpartan-Bold.otf",
                "extrabold": "LeagueSpartan-ExtraBold.otf",
                "black": "LeagueSpartan-Black.otf",
            },
        ),
    ),
    Typeface(
        "Bebas Neue",
        Category.DISPLAY,
        "fonts-bebas-neue",
        "tall, condensed capitals (it draws every letter as a capital): loud posters, "
        "events, sport",
        _faces(
            _BEBAS,
            {
                "light": "BebasNeue-Light.otf",
                "regular": "BebasNeue-Regular.otf",
                "bold": "BebasNeue-Bold.otf",
            },
        ),
    ),
    Typeface(
        "Roboto Condensed",
        Category.DISPLAY,
        "fonts-roboto-unhinted",
        "narrow and sturdy: long headlines in little width, news and data",
        _faces(
            _ROBOTO,
            {
                "light": "RobotoCondensed-Light.ttf",
                "regular": "RobotoCondensed-Regular.ttf",
                "medium": "RobotoCondensed-Medium.ttf",
                "bold": "RobotoCondensed-Bold.ttf",
                "italic": "RobotoCondensed-Italic.ttf",
                "bold-italic": "RobotoCondensed-BoldItalic.ttf",
            },
        ),
    ),
    Typeface(
        "Sora",
        Category.DISPLAY,
        "fonts-sora",
        "wide and modern: technology, developer tools, fintech",
        _faces(
            _SORA,
            {
                "thin": "Sora-Thin.otf",
                "extralight": "Sora-ExtraLight.otf",
                "light": "Sora-Light.otf",
                "regular": "Sora-Regular.otf",
                "semibold": "Sora-SemiBold.otf",
                "bold": "Sora-Bold.otf",
                "extrabold": "Sora-ExtraBold.otf",
            },
        ),
    ),
    Typeface(
        "Comfortaa",
        Category.DISPLAY,
        "fonts-comfortaa",
        "rounded and soft: friendly consumer apps, kids, wellbeing",
        _faces(
            _COMFORTAA,
            {
                "light": "Comfortaa-Light.ttf",
                "regular": "Comfortaa-Regular.ttf",
                "bold": "Comfortaa-Bold.ttf",
            },
        ),
    ),
    Typeface(
        "Inter",
        Category.SANS,
        "fonts-inter",
        "neutral and precise: interfaces, SaaS, any text; heavy weights make clean headlines",
        _faces(
            _INTER,
            {
                "thin": "Inter-Thin.otf",
                "extralight": "Inter-ExtraLight.otf",
                "light": "Inter-Light.otf",
                "regular": "Inter-Regular.otf",
                "medium": "Inter-Medium.otf",
                "semibold": "Inter-SemiBold.otf",
                "bold": "Inter-Bold.otf",
                "extrabold": "Inter-ExtraBold.otf",
                "black": "Inter-Black.otf",
                "italic": "Inter-Italic.otf",
                "bold-italic": "Inter-BoldItalic.otf",
            },
        ),
    ),
    Typeface(
        "Manrope",
        Category.SANS,
        "fonts-manrope",
        "modern, slightly rounded: startups, productivity, calm technology",
        _faces(
            _MANROPE,
            {
                "extralight": "Manrope-ExtraLight.ttf",
                "light": "Manrope-Light.ttf",
                "regular": "Manrope-Regular.ttf",
                "medium": "Manrope-Medium.ttf",
                "semibold": "Manrope-SemiBold.ttf",
                "bold": "Manrope-Bold.ttf",
                "extrabold": "Manrope-ExtraBold.ttf",
            },
        ),
    ),
    Typeface(
        "Open Sans",
        Category.SANS,
        "fonts-open-sans",
        "open and friendly: plain, readable text for everyone",
        _faces(
            _OPEN_SANS,
            {
                "light": "OpenSans-Light.ttf",
                "regular": "OpenSans-Regular.ttf",
                "semibold": "OpenSans-Semibold.ttf",
                "bold": "OpenSans-Bold.ttf",
                "extrabold": "OpenSans-ExtraBold.ttf",
                "italic": "OpenSans-Italic.ttf",
                "bold-italic": "OpenSans-BoldItalic.ttf",
            },
        ),
    ),
    Typeface(
        "Roboto",
        Category.SANS,
        "fonts-roboto-unhinted",
        "familiar and mechanical: Android, utilities, plain information",
        _faces(
            f"{_ROBOTO}/RobotoTTF",
            {
                "thin": "Roboto-Thin.ttf",
                "light": "Roboto-Light.ttf",
                "regular": "Roboto-Regular.ttf",
                "medium": "Roboto-Medium.ttf",
                "bold": "Roboto-Bold.ttf",
                "black": "Roboto-Black.ttf",
                "italic": "Roboto-Italic.ttf",
                "bold-italic": "Roboto-BoldItalic.ttf",
            },
        ),
    ),
    Typeface(
        "Lato",
        Category.SANS,
        "fonts-lato",
        "warm and rounded: lifestyle, health, services",
        _faces(
            _LATO,
            {
                "thin": "Lato-Thin.ttf",
                "light": "Lato-Light.ttf",
                "regular": "Lato-Regular.ttf",
                "medium": "Lato-Medium.ttf",
                "semibold": "Lato-Semibold.ttf",
                "bold": "Lato-Bold.ttf",
                "extrabold": "Lato-Heavy.ttf",
                "black": "Lato-Black.ttf",
                "italic": "Lato-Italic.ttf",
                "bold-italic": "Lato-BoldItalic.ttf",
            },
        ),
    ),
    Typeface(
        "Noto Sans",
        Category.SANS,
        "fonts-noto-core",
        "plain; covers the most scripts (Greek, Cyrillic and more): the fallback for any "
        "letter the others lack",
        _faces(
            _NOTO,
            {
                "regular": "NotoSans-Regular.ttf",
                "bold": "NotoSans-Bold.ttf",
                "italic": "NotoSans-Italic.ttf",
                "bold-italic": "NotoSans-BoldItalic.ttf",
            },
        ),
    ),
    Typeface(
        "EB Garamond",
        Category.SERIF,
        "fonts-ebgaramond",
        "classic book face: elegant, literary, premium, quotes (regular and italic only)",
        _faces(
            _GARAMOND,
            {"regular": "EBGaramond12-Regular.otf", "italic": "EBGaramond12-Italic.otf"},
        ),
    ),
    Typeface(
        "Vollkorn",
        Category.SERIF,
        "fonts-vollkorn",
        "warm and sturdy, strong in heavy weights: editorial headlines, food, craft",
        _faces(
            _VOLLKORN,
            {
                "regular": "Vollkorn-Regular.ttf",
                "medium": "Vollkorn-Medium.ttf",
                "semibold": "Vollkorn-SemiBold.ttf",
                "bold": "Vollkorn-Bold.ttf",
                "extrabold": "Vollkorn-ExtraBold.ttf",
                "black": "Vollkorn-Black.ttf",
                "italic": "Vollkorn-Italic.ttf",
                "bold-italic": "Vollkorn-BoldItalic.ttf",
            },
        ),
    ),
    Typeface(
        "Roboto Slab",
        Category.SERIF,
        "fonts-roboto-slab",
        "slab serif: solid, dependable, a little retro",
        _faces(
            _ROBOTO_SLAB,
            {
                "thin": "RobotoSlab-Thin.otf",
                "light": "RobotoSlab-Light.otf",
                "regular": "RobotoSlab-Regular.otf",
                "bold": "RobotoSlab-Bold.otf",
            },
        ),
    ),
    Typeface(
        "Noto Serif",
        Category.SERIF,
        "fonts-noto-core",
        "plain, readable serif for text; covers many scripts",
        _faces(
            _NOTO,
            {
                "regular": "NotoSerif-Regular.ttf",
                "bold": "NotoSerif-Bold.ttf",
                "italic": "NotoSerif-Italic.ttf",
                "bold-italic": "NotoSerif-BoldItalic.ttf",
            },
        ),
    ),
    Typeface(
        "JetBrains Mono",
        Category.MONO,
        "fonts-jetbrains-mono",
        "code, terminals, developer products",
        _faces(
            _JETBRAINS,
            {
                "light": "JetBrainsMono-Light.ttf",
                "regular": "JetBrainsMono-Regular.ttf",
                "medium": "JetBrainsMono-Medium.ttf",
                "semibold": "JetBrainsMono-SemiBold.ttf",
                "bold": "JetBrainsMono-Bold.ttf",
                "extrabold": "JetBrainsMono-ExtraBold.ttf",
                "italic": "JetBrainsMono-Italic.ttf",
                "bold-italic": "JetBrainsMono-BoldItalic.ttf",
            },
        ),
    ),
    Typeface(
        "Fira Code",
        Category.MONO,
        "fonts-firacode",
        "code with a softer, rounder look",
        _faces(
            _FIRA_CODE,
            {
                "light": "FiraCode-Light.ttf",
                "regular": "FiraCode-Regular.ttf",
                "medium": "FiraCode-Medium.ttf",
                "semibold": "FiraCode-SemiBold.ttf",
                "bold": "FiraCode-Bold.ttf",
            },
        ),
    ),
)


def typeface_named(name: str) -> Typeface | None:
    """The catalogue's family called ``name``, ignoring case and spacing, or ``None``."""
    wanted = _key(name)
    return next((typeface for typeface in TYPEFACES if _key(typeface.name) == wanted), None)


def packages() -> frozenset[str]:
    """The Debian packages the catalogue's fonts come from."""
    return frozenset(typeface.package for typeface in TYPEFACES)


def catalogue_data() -> dict[str, Any]:
    """The catalogue as the sandbox's typography helper reads it (``typefaces.json``)."""
    return {
        "styles": list(STYLES),
        "typefaces": [
            {
                "name": typeface.name,
                "category": typeface.category.value,
                "faces": {style: typeface.faces[style] for style in typeface.styles},
            }
            for typeface in TYPEFACES
        ],
    }


# ------------------------------------------------------------------------------ brand fonts
# Well-known families the sandbox does not have, and the catalogue's nearest in look.
_NEAREST: Mapping[str, str] = {
    "montserrat": "League Spartan",
    "poppins": "League Spartan",
    "futura": "League Spartan",
    "gotham": "League Spartan",
    "proxima nova": "Manrope",
    "oswald": "Bebas Neue",
    "anton": "Bebas Neue",
    "league gothic": "Bebas Neue",
    "barlow condensed": "Roboto Condensed",
    "space grotesk": "Sora",
    "dm sans": "Manrope",
    "plus jakarta sans": "Manrope",
    "nunito": "Comfortaa",
    "quicksand": "Comfortaa",
    "helvetica": "Inter",
    "helvetica neue": "Inter",
    "arial": "Inter",
    "sf pro": "Inter",
    "san francisco": "Inter",
    "system ui": "Inter",
    "segoe ui": "Open Sans",
    "source sans pro": "Open Sans",
    "noto sans": "Noto Sans",
    "work sans": "Lato",
    "raleway": "Lato",
    "ibm plex sans": "Inter",
    "playfair display": "EB Garamond",
    "cormorant": "EB Garamond",
    "garamond": "EB Garamond",
    "libre baskerville": "EB Garamond",
    "baskerville": "EB Garamond",
    "lora": "Vollkorn",
    "merriweather": "Vollkorn",
    "georgia": "Noto Serif",
    "times new roman": "Noto Serif",
    "ibm plex serif": "Noto Serif",
    "source serif pro": "Noto Serif",
    "ibm plex mono": "JetBrains Mono",
    "source code pro": "JetBrains Mono",
    "roboto mono": "JetBrains Mono",
    "sf mono": "JetBrains Mono",
    "menlo": "JetBrains Mono",
    "consolas": "JetBrains Mono",
    "courier new": "JetBrains Mono",
}


@dataclass(frozen=True)
class BrandTypeface:
    """A brand font and the catalogue family the studio sets it in."""

    brand: str
    use: str
    exact: bool


def nearest_typeface(brand_font: str) -> BrandTypeface:
    """The catalogue family to set ``brand_font`` in: itself when the sandbox has it, else the
    nearest in look, else one of its kind (mono, slab, serif), else Inter.

    ``brand_font`` is a brand kit's entry, a family with its role perhaps ("Montserrat
    (headline)"); only the family before the role is read.
    """
    family = brand_font.split("(")[0].strip()
    found = typeface_named(family)
    if found is not None:
        return BrandTypeface(brand_font, found.name, True)
    key = _key(family)
    near = _NEAREST.get(key) or next((use for known, use in _NEAREST.items() if known in key), None)
    return BrandTypeface(brand_font, near or _of_its_kind(key), False)


def _of_its_kind(key: str) -> str:
    if any(word in key for word in ("mono", "code", "courier")):
        return "JetBrains Mono"
    if "slab" in key:
        return "Roboto Slab"
    if "serif" in key and "sans" not in key:
        return "Noto Serif"
    return "Inter"


def brand_typefaces_data(brand_fonts: Sequence[str]) -> list[dict[str, Any]]:
    """What the models are told of the brand kit's fonts: the catalogue family for each."""
    return [
        {"brand_font": found.brand, "use": found.use, "in_the_sandbox": found.exact}
        for found in map(nearest_typeface, brand_fonts)
        if found.brand.strip()
    ]


def _key(name: str) -> str:
    return " ".join(name.casefold().replace("-", " ").replace("_", " ").split())


# ------------------------------------------------------------------------------- the guide
def _catalogue_lines() -> str:
    lines = []
    for category in Category:
        for typeface in (t for t in TYPEFACES if t.category == category):
            lines.append(
                f"- {typeface.name} ({category.value}; {', '.join(typeface.styles)}): "
                f"{typeface.mood}"
            )
    return "\n".join(lines)


def typography_guide() -> str:
    """The typography guide the studio's prompts carry, built from the catalogue."""
    return f"""\
Typography (when code sets words: run_python, render_html, or a layout)
- Use only these fonts, by name; every one renders the Turkish letters (ğ ş ı İ ç ö ü):
{_catalogue_lines()}
- Pair two faces at most: one display or heavy sans for the headline, one sans or serif for \
the rest. Never more than two families in one image.
- Make the hierarchy obvious: the headline clearly dominates; for short copy its letters are \
about 7 to 10% of the image's height, a second line about half that.
- Contrast: dark words on a light, calm area or light words on a dark one; if the picture is \
busy where the words go, move them, or add a soft shadow or a subtle local gradient only as \
much as legibility needs.
- Keep every word inside the safe margin (safe_margin_px from each edge) and wrap long lines; \
never let a word touch an edge or run over the subject.
- Every letter must render as written, with its accents and special letters: never replace \
ğ, ş, ı, İ with g, s, i, I.
- Brand fonts: the brand kit's font when it is in this list; otherwise the family \
brand_typefaces names for it (the nearest in look).
- In run_python: `from studio_type import fit_text, font` (always importable). fit_text(image, \
text, box, font_name, weight=...) finds the largest size at which the text, wrapped, fits the \
box (left, top, right, bottom), draws it and returns the size and the text's bounding box; \
font(name, weight, size) gives a Pillow font of the catalogue. Never compute font sizes by \
hand or open font files by path.
- In render_html: font-family with these names (font-family: "League Spartan"); the browser \
has them installed. No web fonts."""


def main() -> int:
    """Print ``catalogue_data`` as the sandbox's ``typefaces.json``."""
    sys.stdout.write(json.dumps(catalogue_data(), indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
