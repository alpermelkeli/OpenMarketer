"""The programmatic checks of a studio candidate: its file against its platform, its words.

``check_image`` reads an image's header and measures it against the platform's
``PlatformSpec``: its size and aspect ratio, its bytes and its format. ``copy_matches``
compares the text an OCR engine read in the image with the exact lines the request gave, after
the same normalisation of both (case, Unicode forms, punctuation, whitespace), and says which
lines OCR confirmed and, of each it did not, what it read nearest to it. OCR is evidence, not
the judge: it misses large light words on photos and splits long ones, so a line it did not
confirm is for the visual verifier to read letter by letter (``studio_agent/rules.py``), while
a line it read exactly is confirmed.

What is not checked here: the platform's safe margin. The OCR the sandbox gives reads text
without positions, so whether the text keeps clear of the edges is the visual verifier's
question for now. Contrast and the distance from the palette are not measured either.

Every function is pure, over bytes and text; Pillow reads the header and nothing else is
decoded, and it is loaded only when an image is checked. Nothing here decides what a failure
leads to: that is ``studio_agent/rules.py``.
"""

from __future__ import annotations

import difflib
import io
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

from openmarketer_core.studio.platforms import PlatformSpec

# The failures a check names; the decide rule reads them as keys. ``COPY`` is named by the
# verify node, when the verifier's reading of a line OCR did not confirm is not that line.
WRONG_SIZE = "size"
WRONG_FORMAT = "format"
TOO_MANY_BYTES = "bytes"
UNREADABLE = "unreadable"
COPY = "copy"

# How close a stretch of the read text must be to a line to be shown as what OCR read of it.
MISSPELLED_AT = 0.6
# The most of the read text a report repeats for one line.
MAX_READ_CHARS = 160

_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_NOT_WORD = re.compile(r"[^\w]+")


@dataclass(frozen=True)
class ImageCheck:
    """How an image measures against its platform; ``failures`` is empty when it fits."""

    width: int
    height: int
    size_bytes: int
    mime: str
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class Unconfirmed:
    """A line of copy OCR did not read exactly: what it should read, and the nearest text OCR
    read (empty when nothing read close to it)."""

    expected: str
    read: str


@dataclass(frozen=True)
class CopyCheck:
    """Which lines of copy OCR read exactly, and what it read of each of the others."""

    confirmed: tuple[str, ...] = ()
    unconfirmed: tuple[Unconfirmed, ...] = field(default=())

    @property
    def all_confirmed(self) -> bool:
        return not self.unconfirmed

    def as_data(self) -> dict[str, object]:
        return {
            "confirmed": list(self.confirmed),
            "unconfirmed": [{"expected": u.expected, "read": u.read} for u in self.unconfirmed],
        }


def check_image(data: bytes, spec: PlatformSpec) -> ImageCheck:
    """Measure ``data`` against ``spec``: its size in pixels, its bytes and its format."""
    # Imported here, not with the module: the studio's rules name this module's failure keys,
    # and the workflow that loads the rules has no use for Pillow.
    from PIL import Image, UnidentifiedImageError

    from openmarketer_core.studio.output_check import sniffed_format

    announced = sniffed_format(data)
    if announced is None:
        return ImageCheck(0, 0, len(data), "", (UNREADABLE,))
    try:
        with Image.open(io.BytesIO(data), formats=[announced]) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return ImageCheck(0, 0, len(data), "", (UNREADABLE,))
    mime = _MIMES[announced]
    failures: list[str] = []
    if (width, height) != (spec.width, spec.height):
        failures.append(WRONG_SIZE)
    if mime not in spec.formats:
        failures.append(WRONG_FORMAT)
    if len(data) > spec.max_bytes:
        failures.append(TOO_MANY_BYTES)
    return ImageCheck(width, height, len(data), mime, tuple(failures))


def normalized(text: str) -> str:
    """``text`` as copy is compared: one Unicode form, folded case, words and single spaces."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_NOT_WORD.sub(" ", folded).split())


# Letters that carry their mark in their shape, not as a combining mark NFKD would split off.
_UNMARKED = str.maketrans({"ı": "i", "ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe"})


def without_marks(text: str) -> str:
    """``text`` (already normalised) with its accents and special letters taken to plain ones:
    ç to c, ğ to g, ı to i, ş to s, é to e."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_UNMARKED))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _read_in(read: str, read_words: list[str], wanted: str) -> bool:
    return _contains(read, wanted) or _contains_through_noise(read_words, wanted)


def copy_matches(ocr_text: str, exact_copy: Sequence[str]) -> CopyCheck:
    """Which lines of ``exact_copy`` OCR read in ``ocr_text``, compared normalised.

    The read text is taken as one run of words, because OCR breaks lines where the image does
    and not where the copy does. A line read right but for its accents and special letters is
    not confirmed: OCR misses them in large and bold type, but a wrong letter reads the same,
    so the verifier, looking at the image, judges them. Of a line not confirmed, the nearest
    stretch of as many words is kept when it reads close to the line, so the verifier is told
    what OCR made of it.
    """
    read = normalized(ocr_text)
    read_words = read.split()
    read_unmarked = without_marks(read)
    confirmed: list[str] = []
    unconfirmed: list[Unconfirmed] = []
    for line in exact_copy:
        wanted = normalized(line)
        if not wanted or _read_in(read, read_words, wanted):
            confirmed.append(line)
            continue
        if _read_in(read_unmarked, read_unmarked.split(), without_marks(wanted)):
            # Right but for its marks: OCR often drops them in large type, but a wrong letter
            # (c for ç) reads the same, so the verifier reads this line letter by letter.
            closest, _ = _closest_stretch(read_words, wanted)
            unconfirmed.append(Unconfirmed(line, closest[:MAX_READ_CHARS]))
            continue
        closest, ratio = _closest_stretch(read_words, wanted)
        nearest = closest[:MAX_READ_CHARS] if ratio >= MISSPELLED_AT else ""
        unconfirmed.append(Unconfirmed(line, nearest))
    return CopyCheck(tuple(confirmed), tuple(unconfirmed))


def _contains(read: str, wanted: str) -> bool:
    return f" {wanted} " in f" {read} "


# OCR reads specks, scribbles and textures as short tokens ("o", "ae") that land between the
# words of a line. Between two words of a line, up to this many such tokens are passed over;
# a word of the line itself must still be read exactly.
NOISE_TOKENS_BETWEEN_WORDS = 2
NOISE_TOKEN_MAX_CHARS = 3


def _contains_through_noise(read_words: list[str], wanted: str) -> bool:
    """Whether the words of ``wanted`` are read in order, with at most a few short tokens of
    noise between two of them."""
    words = wanted.split()
    for start, word in enumerate(read_words):
        if word != words[0]:
            continue
        position, matched = start + 1, 1
        while matched < len(words):
            skipped = 0
            while (
                position < len(read_words)
                and read_words[position] != words[matched]
                and len(read_words[position]) <= NOISE_TOKEN_MAX_CHARS
                and skipped < NOISE_TOKENS_BETWEEN_WORDS
            ):
                position, skipped = position + 1, skipped + 1
            if position >= len(read_words) or read_words[position] != words[matched]:
                break
            position, matched = position + 1, matched + 1
        if matched == len(words):
            return True
    return False


def _closest_stretch(read_words: list[str], wanted: str) -> tuple[str, float]:
    """The stretch of the read words nearest ``wanted``, of its length give or take one word."""
    count = len(wanted.split())
    best, best_ratio = "", 0.0
    for size in {max(1, count - 1), count, count + 1}:
        for start in range(max(1, len(read_words) - size + 1)):
            stretch = " ".join(read_words[start : start + size])
            ratio = difflib.SequenceMatcher(None, stretch, wanted).ratio()
            if ratio > best_ratio:
                best, best_ratio = stretch, ratio
    return best, best_ratio
