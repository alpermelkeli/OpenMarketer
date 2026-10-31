"""What a platform asks of a post's text: how it counts length, and what the text may hold.

A platform is the brief's (``brief.WritingPlatform``), and each has its ``PlatformRules``: the
most a text may count, how the platform counts it, how many hashtags it may carry, and whether
it may hold a link or a mention. The writer's rules (``writer_agent/rules.py``) build both the
sentences the model is told and the checks of a variant from these values, so the two cannot
drift apart.

X counts a text as its own library, twitter-text (version 3), does: the text is normalized to
NFC, and each code point weighs 1 when it is in one of X's ranges of weight 1 (Latin, Greek,
Cyrillic, Hebrew, Arabic and the other scripts below U+1100, and a few runs of punctuation)
and 2 otherwise, so CJK counts 2. An emoji counts 2 whatever it is made of: a sequence joined
with zero-width joiners, a skin tone, a flag or a keycap is one emoji. A post may weigh 280.
X counts a link as 23 whatever its length; links are refused outright here, so they are counted
as their characters. The emoji ranges are an approximation of Unicode's pictographic ranges,
wide enough that a single code point outside them is weighed 2 all the same.

Links and mentions are left out entirely: the profile has neither, and an allow-list of the
project's own links and accounts, entered by a person, can come later. A link is a web address
with a scheme, one that starts with ``www.``, or a bare domain under a common top-level domain,
since X links those too.

This module reaches no database and no network, and knows no platform's API.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

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
_TOP_LEVEL_DOMAINS = (
    "com|net|org|io|ai|app|dev|co|me|xyz|so|sh|gg|tv|ly|to|us|uk|de|fr|es|it|nl|eu|ca|au|in|"
    "jp|cn|ru|br|tr|info|biz|tech|site|online|cloud|page|link|store|shop|blog|news|design|"
    "studio|world|live|life|today|space|fun|tools|software|email|club|pro|one"
)
LINK = re.compile(
    r"(?:\b[a-z][a-z0-9+.-]*://\S+)"
    r"|(?:\bwww\.\S+)"
    rf"|(?:\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:{_TOP_LEVEL_DOMAINS})\b(?:/\S*)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlatformRules:
    """What one platform asks of a post's text.

    ``count`` is how the platform counts a text's length and ``counting`` the sentence that
    says so to a model; ``max_length`` is the most it may count.
    """

    name: str
    max_length: int
    count: Callable[[str], int]
    counting: str
    max_hashtags: int
    allows_links: bool
    allows_mentions: bool


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


def hashtags(text: str) -> list[str]:
    """The hashtags of ``text``, as a platform reads them, in order."""
    return [match.group(0) for match in HASHTAG.finditer(text)]


def mentions(text: str) -> list[str]:
    """The @mentions of ``text``, in order."""
    return [match.group(0) for match in MENTION.finditer(text)]


def links(text: str) -> list[str]:
    """The web addresses of ``text``, in order: with a scheme, from ``www.``, or a bare domain."""
    return [match.group(0) for match in LINK.finditer(text)]


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
        ),
    }
)


def platform_rules(platform: WritingPlatform) -> PlatformRules:
    """What ``platform`` asks of a post's text."""
    return PLATFORM_RULES[platform]


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
