"""What a platform asks of a post: how it counts length, what the text may hold, which images fit.

A platform is the brief's (``brief.WritingPlatform``), and each has its ``PlatformRules``: the
most a text may count, how the platform counts it, how many hashtags it may carry, whether it
may hold a link or a mention, the habits of a good post on it, and the studio's platforms whose
images a post on it may go with. The writer's rules (``writer_agent/rules.py``) build both the
sentences the model is told and the checks of a variant from these values, so the two cannot
drift apart; the habits are told, never checked.

X counts a text as its own library, twitter-text (version 3), does: the text is normalized to
NFC, and each code point weighs 1 when it is in one of X's ranges of weight 1 (Latin, Greek,
Cyrillic, Hebrew, Arabic and the other scripts below U+1100, and a few runs of punctuation)
and 2 otherwise, so CJK counts 2. An emoji counts 2 whatever it is made of: a sequence joined
with zero-width joiners, a skin tone, a flag or a keycap is one emoji. A post may weigh 280.
X counts a link as 23 whatever its length; links are refused outright here, so they are counted
as their characters. The emoji ranges are an approximation of Unicode's pictographic ranges,
wide enough that a single code point outside them is weighed 2 all the same.

Links and mentions are left out entirely: the profile has neither, and an allow-list of the
project's own links and accounts, entered by a person, can come later. The rule is wide rather
than a list of top-level domains, since new ones keep appearing and X links a bare domain under
any of them: the text is first read as an address would be (NFKC, so full-width letters and
dots become plain ones; the ideographic full stops read as a dot; invisible format characters
and combining marks dropped), and then a link is anything with a scheme, anything that starts
with ``www.``, and any ``label.label`` whose last label is two or more letters of any script, or
a punycode label. It refuses product names written with a dot, such as "Node.js": telling them
from a domain would take a list again, and the writer is told to write such a name without its
dot. It also refuses a full stop with no space after it, between two words of any script.

Instagram takes a feed caption of at most 2,200 characters and 30 hashtags, and makes no link
in a caption clickable: a post sends readers to the "link in bio" instead, which the writer is
told, and an address in a caption is refused like any other link. Instagram does not publish
how it counts, so a caption is counted as its NFC form's UTF-16 code units, as the app's text
field counts it: an emoji counts 2 or more, never less than Instagram would count it. An
Instagram post is an image with its caption, so its brief names an asset
(``brief.PLATFORMS_WITH_IMAGE``), and only an image made for Instagram fits it; an X post may
go with an image made for any platform.

This module reaches no database and no network, and knows no platform's API.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from openmarketer_core.studio.platforms import Platform
from openmarketer_core.writing.brief import WritingPlatform

# twitter-text's configuration v3: the ranges of code points that weigh 1, inclusive.
X_WEIGHT_ONE_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x10FF),
    (0x2000, 0x200D),
    (0x2010, 0x201F),
    (0x2032, 0x2037),
)
X_MAX_WEIGHTED_LENGTH = 280
X_MAX_HASHTAGS = 2
INSTAGRAM_MAX_CAPTION_LENGTH = 2200
INSTAGRAM_MAX_HASHTAGS = 30

_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2300, 0x23FF),
    (0x2B00, 0x2BFF),
    (0x2190, 0x21FF),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
)
_REGIONAL_INDICATORS = (0x1F1E6, 0x1F1FF)
_SKIN_TONES = (0x1F3FB, 0x1F3FF)
_TAGS = (0xE0020, 0xE007F)
_ZWJ = 0x200D
_VARIATION_SELECTORS = (0xFE0E, 0xFE0F)
_EMOJI_PRESENTATION = 0xFE0F
_KEYCAP = 0x20E3
_KEYCAP_BASES = frozenset("0123456789#*")
# Text characters that are an emoji only when the emoji presentation follows them.
_EMOJI_WITH_PRESENTATION = frozenset("©®‼⁉™ℹ")

HASHTAG = re.compile(r"(?:^|(?<=[^\w&]))[#＃](?=\w*[^\W\d_])\w+")
MENTION = re.compile(r"(?:^|(?<=[^\w@＠!#$%&*]))[@＠][A-Za-z0-9_]{1,15}")
# Dots an address may be written with that NFKC leaves as they are: the ideographic full stop
# (U+3002), its half-width form (U+FF61, which NFKC turns into U+3002) and its vertical form.
_DOTS = str.maketrans({"\u3002": ".", "\uff61": ".", "\ufe12": "."})
# A label: letters and digits of any script, hyphens and underscores inside.
_LABEL = r"[^\W_](?:[\w-]*[^\W_])?"
# The last label: two or more letters of any script, or punycode; nothing word-like after it.
_LAST_LABEL = r"(?:[^\W\d_]{2,}|xn--[\w-]+)(?![^\W_])"
LINK = re.compile(
    r"(?:[^\W_][\w+.-]*://\S*)"
    r"|(?:www\.\S*)"
    rf"|(?:{_LABEL}(?:\.{_LABEL})*\.{_LAST_LABEL}(?:/\S*)?)",
    re.IGNORECASE,
)
# What the writer is told of ``LINK``; the two say the same thing.
NO_LINKS_TOLD = (
    "No link or web address of any kind: nothing with a scheme such as https://, nothing that "
    "starts with www., and no two words joined by a dot with no space between them, such as "
    "example.com, in any script and with any kind of dot. Write a product name that has a dot "
    "in it, such as Node.js, without the dot and what follows it (Node), and leave a space "
    "after every full stop."
)


@dataclass(frozen=True)
class PlatformRules:
    """What one platform asks of a post.

    ``count`` is how the platform counts a text's length and ``counting`` the sentence that
    says so to a model; ``max_length`` is the most it may count. ``habits`` are the sentences
    of what a good post on it does, told to the writer and the critic and checked by neither
    code nor a refusal. ``image_platforms`` are the studio's platforms whose images a post on
    it may go with.
    """

    name: str
    max_length: int
    count: Callable[[str], int]
    counting: str
    max_hashtags: int
    allows_links: bool
    allows_mentions: bool
    image_platforms: frozenset[Platform]
    habits: tuple[str, ...] = ()


def x_weighted_length(text: str) -> int:
    """The length of ``text`` as X counts it (twitter-text v3, without its links' weight)."""
    points = [ord(char) for char in unicodedata.normalize("NFC", text)]
    weight = 0
    index = 0
    while index < len(points):
        end = _emoji_end(points, index)
        if end > index:
            weight += 2
            index = end
            continue
        weight += 1 if _weighs_one(points[index]) else 2
        index += 1
    return weight


def instagram_length(text: str) -> int:
    """The length of ``text`` as an Instagram caption: the UTF-16 code units of its NFC form."""
    return len(unicodedata.normalize("NFC", text).encode("utf-16-le")) // 2


def hashtags(text: str) -> list[str]:
    """The hashtags of ``text``, as a platform reads them, in order."""
    return [match.group(0) for match in HASHTAG.finditer(text)]


def mentions(text: str) -> list[str]:
    """The @mentions of ``text``, in order."""
    return [match.group(0) for match in MENTION.finditer(text)]


def links(text: str) -> list[str]:
    """The web addresses of ``text``, in order: with a scheme, from ``www.``, or a bare domain.

    Each is quoted as it reads once normalized for the check, which is how a platform reads it.
    """
    return [match.group(0) for match in LINK.finditer(_as_an_address_reads(text))]


def _as_an_address_reads(text: str) -> str:
    """``text`` as a host name would be read: NFKC, every kind of full stop a dot, and without
    the invisible characters and combining marks that would otherwise split a label."""
    folded = unicodedata.normalize("NFKC", text).translate(_DOTS)
    return "".join(char for char in folded if not _invisible(char))


def _invisible(char: str) -> bool:
    category = unicodedata.category(char)
    return category == "Cf" or category.startswith("M")


PLATFORM_RULES: Mapping[WritingPlatform, PlatformRules] = MappingProxyType(
    {
        WritingPlatform.X: PlatformRules(
            name="X",
            max_length=X_MAX_WEIGHTED_LENGTH,
            count=x_weighted_length,
            counting=(
                "X counts most letters, digits and punctuation as 1, and CJK characters "
                "and every emoji as 2"
            ),
            max_hashtags=X_MAX_HASHTAGS,
            allows_links=False,
            allows_mentions=False,
            image_platforms=frozenset(Platform),
        ),
        WritingPlatform.INSTAGRAM: PlatformRules(
            name="Instagram",
            max_length=INSTAGRAM_MAX_CAPTION_LENGTH,
            count=instagram_length,
            counting=(
                "Instagram counts every character as 1, line breaks included, and an emoji as "
                "2 or more"
            ),
            max_hashtags=INSTAGRAM_MAX_HASHTAGS,
            allows_links=False,
            allows_mentions=False,
            image_platforms=frozenset(
                {Platform.INSTAGRAM_POST, Platform.INSTAGRAM_PORTRAIT, Platform.INSTAGRAM_STORY}
            ),
            habits=(
                "The caption goes with its image: its first line is what shows before the "
                "caption is cut off, so make it carry the post.",
                "Line breaks are welcome: short paragraphs read better than one block.",
                "Prefer a few relevant hashtags (three to five), all at the end of the caption, "
                "after a blank line; never one in the middle of a sentence.",
                "A link in a caption cannot be clicked: to send readers to the product, write "
                '"link in bio" instead of an address.',
            ),
        ),
    }
)


def platform_rules(platform: WritingPlatform) -> PlatformRules:
    """What ``platform`` asks of a post's text."""
    return PLATFORM_RULES[platform]


def image_fits(platform: WritingPlatform, image_platform: Platform) -> bool:
    """Whether a post on ``platform`` may go with an image the studio made for
    ``image_platform``."""
    return image_platform in PLATFORM_RULES[platform].image_platforms


def _weighs_one(point: int) -> bool:
    return any(low <= point <= high for low, high in X_WEIGHT_ONE_RANGES)


def _within(point: int, bounds: tuple[int, int]) -> bool:
    return bounds[0] <= point <= bounds[1]


def _pictographic(point: int) -> bool:
    return any(low <= point <= high for low, high in _EMOJI_RANGES)


def _emoji_end(points: list[int], start: int) -> int:
    """Where the emoji that starts at ``start`` ends, or ``start`` when none starts there."""
    first = points[start]
    following = points[start + 1] if start + 1 < len(points) else None
    if _within(first, _REGIONAL_INDICATORS):
        if following is not None and _within(following, _REGIONAL_INDICATORS):
            return start + 2
        return start + 1
    if chr(first) in _KEYCAP_BASES:
        index = start + 1
        if index < len(points) and points[index] == _EMOJI_PRESENTATION:
            index += 1
        return index + 1 if index < len(points) and points[index] == _KEYCAP else start
    if chr(first) in _EMOJI_WITH_PRESENTATION:
        if following != _EMOJI_PRESENTATION:
            return start
    elif not _pictographic(first):
        return start
    return _sequence_end(points, start + 1)


def _sequence_end(points: list[int], index: int) -> int:
    """Past the modifiers of an emoji, and past every emoji a zero-width joiner joins to it."""
    while index < len(points):
        point = points[index]
        if (
            _within(point, _VARIATION_SELECTORS)
            or _within(point, _SKIN_TONES)
            or _within(point, _TAGS)
            or point == _KEYCAP
        ):
            index += 1
        elif point == _ZWJ and index + 1 < len(points) and _pictographic(points[index + 1]):
            index += 2
        else:
            break
    return index
